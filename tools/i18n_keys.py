"""Lists every English source string the app can show, and reports which ones lack a translation.
Usage: python3 tools/i18n_keys.py [--missing]"""
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CALLS = {"t", "tt", "T", "say", "L", "notify", "sms_user", "flash"}
PY_LISTS = {"MEMBER_ITEMS", "COORD_ITEMS", "NEEDS", "DENY_REASONS", "ERR"}


def from_python(path):
    out = set()
    tree = ast.parse(path.read_text())
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            name = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
            if name in CALLS and n.args:
                for a in n.args[:2 if name in ("notify", "sms_user", "tt") else 1]:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        out.add(a.value)
                    if isinstance(a, ast.IfExp):
                        for b in (a.body, a.orelse):
                            if isinstance(b, ast.Constant) and isinstance(b.value, str):
                                out.add(b.value)
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in PY_LISTS for t in n.targets):
            for c in ast.walk(n.value):
                if isinstance(c, ast.Constant) and isinstance(c.value, str):
                    out.add(c.value)
    return out


BLOCK = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)
STR = re.compile(r"""(['"])((?:\\.|(?!\1).)+)\1""")
SKIP = re.compile(r"^(#|/|https?:|\.|[a-z_]+\.[a-z_]+|[a-z]+[-:][a-z0-9-]+|[a-z_]+=|%|\d|_)|\.html|\.png|\.svg|\.js|\.css|^[a-z_]+$")


def from_template(path):
    out = set()
    for blk in BLOCK.findall(path.read_text()):
        for _, s in STR.findall(blk):
            s = s.replace("\\'", "'").strip()
            if not s or s[0] in ",)(=" or s.endswith("="):
                continue
            if len(s) < 2 or SKIP.search(s) and not s[0].isupper():
                continue
            if s[0].isupper() or " " in s:
                out.add(s)
    return out


def literal_text(path):
    """Static text between tags is written in English inside templates; those are wrapped in t() already."""
    return set()


def collect():
    keys = set()
    for p in ROOT.glob("*.py"):
        keys |= from_python(p)
    keys |= {n.value for n in ast.walk(ast.parse((ROOT / "legal.py").read_text())) if isinstance(n, ast.Constant) and isinstance(n.value, str) and len(n.value) > 3 and not n.value.startswith("Legal texts") and not n.value.startswith("These are working drafts") and not re.fullmatch(r"[\d-]+", n.value) and not n.value.startswith("!")}
    for p in (ROOT / "templates").rglob("*.html"):
        keys |= from_template(p)
    return {k for k in keys if k and not k.startswith(("<", "#", "!")) and (" " in k or k[0].isupper() or k in ("none", "no show"))}


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    keys = sorted(collect())
    if "--missing" in sys.argv:
        from i18n import TR
        keys = [k for k in keys if k not in TR]
    print("\n".join(keys))
    print(f"# {len(keys)} strings", file=sys.stderr)
