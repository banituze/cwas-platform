"""Chat attachments: accept any file type, store it safely, describe it honestly.
Nothing uploaded is ever executed or rendered as HTML. Only verified images and PDFs are served inline; everything else is a download."""
import csv
import io
import json
import re
import secrets
import struct
import zipfile
from pathlib import Path

from i18n import tt

MAX_FILES = 5
MAX_BYTES = 10 * 1024 * 1024
TEXT_EXT = {"txt", "md", "log", "csv", "tsv", "json", "xml", "yaml", "yml", "ini", "cfg", "conf", "html", "htm", "css", "js", "py", "sql", "rtf", "srt", "vcf", "ics"}


def human(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _ext(name):
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def image_size(d):
    try:
        if d.startswith(b"\x89PNG\r\n\x1a\n"):
            return struct.unpack(">II", d[16:24])
        if d[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", d[6:10])
        if d.startswith(b"BM"):
            w, h = struct.unpack("<ii", d[18:26])
            return w, abs(h)
        if d[:4] == b"RIFF" and d[8:12] == b"WEBP":
            if d[12:16] == b"VP8X":
                return 1 + int.from_bytes(d[24:27], "little"), 1 + int.from_bytes(d[27:30], "little")
            if d[12:16] == b"VP8 ":
                return struct.unpack("<HH", d[26:30])[0] & 0x3FFF, struct.unpack("<HH", d[26:30])[1] & 0x3FFF
            if d[12:16] == b"VP8L":
                b = int.from_bytes(d[21:25], "little")
                return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
        if d.startswith(b"\xff\xd8"):
            i = 2
            while i < len(d) - 9:
                if d[i] != 0xFF:
                    i += 1
                    continue
                m = d[i + 1]
                if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
                    i += 2
                    continue
                ln = struct.unpack(">H", d[i + 2:i + 4])[0]
                if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", d[i + 5:i + 9])
                    return w, h
                i += 2 + ln
    except (struct.error, IndexError):
        pass
    return None


def detect(name, d):
    """Returns (kind, mime). Kind decides how the file is described and whether it can be shown inline."""
    ext = _ext(name)
    if d.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png"
    if d.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg"
    if d[:6] in (b"GIF87a", b"GIF89a"):
        return "image", "image/gif"
    if d[:4] == b"RIFF" and d[8:12] == b"WEBP":
        return "image", "image/webp"
    if d.startswith(b"BM") and ext == "bmp":
        return "image", "image/bmp"
    if d.startswith(b"%PDF"):
        return "pdf", "application/pdf"
    if d[:2] == b"MZ" or d[:4] == b"\x7fELF" or d.startswith(b"#!") or ext in ("exe", "dll", "bat", "cmd", "sh", "msi", "apk", "jar", "scr", "ps1", "vbs"):
        return "executable", "application/octet-stream"
    if d.startswith(b"PK\x03\x04"):
        return {"docx": "document", "xlsx": "spreadsheet", "pptx": "presentation", "odt": "document", "ods": "spreadsheet", "odp": "presentation"}.get(ext, "archive"), "application/zip"
    if d[:2] == b"\x1f\x8b" or d[:6] == b"7z\xbc\xaf'\x1c" or d[:4] == b"Rar!" or d[257:262] == b"ustar":
        return "archive", "application/octet-stream"
    if d[:3] == b"ID3" or d[:2] in (b"\xff\xfb", b"\xff\xf3") or (d[:4] == b"RIFF" and d[8:12] == b"WAVE") or d[:4] in (b"OggS", b"fLaC") or ext in ("mp3", "wav", "m4a", "ogg", "flac", "aac", "opus", "amr"):
        return "audio", "application/octet-stream"
    if d[4:8] == b"ftyp" or d[:4] == b"\x1aE\xdf\xa3" or ext in ("mp4", "mov", "webm", "mkv", "avi", "3gp"):
        return "video", "application/octet-stream"
    head = d[:4096]
    if b"\x00" not in head:
        try:
            head.decode("utf-8")
            return ("csv" if ext in ("csv", "tsv") else "json" if ext == "json" else "text"), "text/plain"
        except UnicodeDecodeError:
            if ext in TEXT_EXT:
                return "text", "text/plain"
    return "other", "application/octet-stream"


def describe(name, d, lang):
    """One honest line about the file, in the reader's language."""
    kind, _ = detect(name, d)
    size = human(len(d))

    def L(s, **kw):
        return tt(s, lang, size=size, **kw)
    ext = _ext(name) or "?"
    try:
        if kind == "image":
            dim = image_size(d)
            return L("Image, {w} x {h} pixels, {size}. I can describe the file but I cannot see the picture.", w=dim[0], h=dim[1]) if dim else L("Image, {size}. I can describe the file but I cannot see the picture.")
        if kind == "pdf":
            pages = len(re.findall(rb"/Type\s*/Page[^s]", d)) or 1
            return L("PDF document, about {n} pages, {size}. I cannot read its text yet.", n=pages)
        if kind in ("text", "csv", "json"):
            txt = d.decode("utf-8", "replace")
            if kind == "csv":
                rows = list(csv.reader(io.StringIO(txt), delimiter="\t" if ext == "tsv" else ","))
                rows = [r for r in rows if r]
                out = L("Table: {rows} rows and {cols} columns. Columns: {names}.", rows=max(0, len(rows) - 1), cols=len(rows[0]) if rows else 0, names=", ".join(c[:18] for c in (rows[0] if rows else [])[:5]))
                for ci, col in enumerate(rows[0] if rows else []):
                    vals = []
                    for r in rows[1:]:
                        try:
                            vals.append(float(r[ci].replace(" ", "").replace(",", ".")))
                        except (ValueError, IndexError):
                            vals = []
                            break
                    if vals:
                        return out + " " + L("Column {col}: total {total}, average {avg}.", col=col[:18], total=f"{sum(vals):,.2f}".rstrip("0").rstrip("."), avg=f"{sum(vals) / len(vals):,.2f}".rstrip("0").rstrip("."))
                return out
            if kind == "json":
                try:
                    obj = json.loads(txt)
                    return L("JSON data with {n} top-level items.", n=len(obj) if isinstance(obj, (list, dict)) else 1)
                except ValueError:
                    pass
            lines = txt.splitlines()
            first = next((l.strip() for l in lines if l.strip()), "")[:90]
            return L("Text file: {lines} lines, {words} words, {size}.", lines=len(lines), words=len(txt.split())) + (" " + L("Starts with: {preview}", preview=first) if first else "")
        if kind in ("document", "spreadsheet", "presentation", "archive") and d.startswith(b"PK"):
            z = zipfile.ZipFile(io.BytesIO(d))
            names = z.namelist()
            if kind == "document" and "word/document.xml" in names:
                xml = z.read("word/document.xml").decode("utf-8", "ignore")
                return L("Word document, about {n} words, {size}.", n=len(re.sub(r"<[^>]+>", " ", xml).split()))
            if kind == "spreadsheet" and "xl/workbook.xml" in names:
                sheets = re.findall(r'<sheet [^>]*name="([^"]+)"', z.read("xl/workbook.xml").decode("utf-8", "ignore"))
                return L("Spreadsheet with {n} sheets: {names}.", n=len(sheets), names=", ".join(sheets[:4]))
            if kind == "presentation":
                return L("Presentation with {n} slides.", n=len([n for n in names if re.match(r"ppt/slides/slide\d+\.xml", n)]))
            return L("Archive with {n} items, for example {names}.", n=len(names), names=", ".join(n[:24] for n in names[:3]))
    except Exception:  # noqa: BLE001 - a damaged file is described generically, never an error page
        return L("File of type {ext}, {size}.", ext=ext)
    if kind == "audio":
        return L("Audio file, {size}. I cannot listen to it, but it is saved in this chat.")
    if kind == "video":
        return L("Video file, {size}. I cannot watch it, but it is saved in this chat.")
    if kind == "executable":
        return L("Program or script file, {size}. It was saved but never opened or run.")
    if kind == "archive":
        return L("Archive file, {size}. I did not unpack it.")
    return L("File of type {ext}, {size}.", ext=ext)


def reply_for(text, files, lang):
    """The assistant's answer to a message that carries files. `files` is a list of (name, bytes)."""
    lines = [tt("I received {n} file(s):", lang, n=len(files))]
    for name, data in files:
        lines.append(f"- {name[:40]}: {describe(name, data, lang)}")
    t = (text or "").lower()
    if any(k in t for k in ("pay", "deposit", "receipt", "refund", "rembours", "famerenana", "reçu", "rosia")):
        lines.append(tt("For a payment problem, tell your coordinator the transaction reference. Files in this chat are not shared with anyone.", lang))
    else:
        lines.append(tt("Saved in this chat. Tell me what you would like to know about it.", lang))
    return "\n".join(lines)[:1500]


def optimize(name, data, limit=2560):
    """A lighter copy of an uploaded photo that looks the same: the camera's orientation is applied, location and camera
    metadata are dropped, anything wider or taller than `limit` px is scaled down (2560 keeps it HD), then JPEG is saved at
    quality 85, PNG losslessly and WebP at 85. Anything else (PDFs, GIFs, animations, files Pillow cannot read) and any copy
    that would not come out smaller keep the original bytes."""
    if detect(name, data)[0] != "image":
        return data
    try:
        from PIL import Image, ImageOps
        im = Image.open(io.BytesIO(data))
        fmt = im.format
        if fmt not in ("JPEG", "PNG", "WEBP") or getattr(im, "is_animated", False):
            return data
        im = ImageOps.exif_transpose(im)
        if max(im.size) > limit:
            im.thumbnail((limit, limit), Image.LANCZOS)
        out = io.BytesIO()
        if fmt == "JPEG":
            im.convert("RGB").save(out, "JPEG", quality=85, optimize=True, progressive=True)
        elif fmt == "PNG":
            im.save(out, "PNG", optimize=True)
        else:
            im.save(out, "WEBP", quality=85, method=4)
        return out.getvalue() if out.tell() < len(data) else data
    except Exception:  # noqa: BLE001 - an unreadable image is kept as it came
        return data


def save(root, user_id, data):
    folder = Path(root) / "uploads" / str(user_id)
    folder.mkdir(parents=True, exist_ok=True)
    fid = secrets.token_hex(12)
    (folder / fid).write_bytes(data)
    return fid


def path_of(root, user_id, fid):
    if not re.fullmatch(r"[0-9a-f]{24}", fid or ""):
        return None
    p = Path(root) / "uploads" / str(user_id) / fid
    return p if p.is_file() else None
