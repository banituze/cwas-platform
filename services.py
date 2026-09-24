"""CWAS business logic. Every rule the SRS names lives here so the web UI, USSD and SMS share one engine."""
import base64
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import statistics
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import has_request_context, request
from sqlalchemy import func

from i18n import tt
from werkzeug.security import generate_password_hash
from models import (AuditLog, Booking, Household, Maintenance, Notification, Setting, SmsLog,
                    WalletTxn, WaterSource, db, utcnow)

TZ = ZoneInfo("Indian/Antananarivo")
HORIZON_DAYS = 7
LITRE_STEPS = (20, 40, 60, 80, 100)
DEFAULT_SETTINGS = {
    "discount_elevated": "30",   # % off the tariff for households flagged elevated priority
    "discount_high": "60",       # % off for the most vulnerable households (business plan: up to 70%)
    "min_deposit": "500",
    "max_deposit": "200000",
    "auto_approve": "0",         # 1 = bookings the AI rates low-risk are approved without a coordinator click
    "cash_enabled": "1",
    "no_show_grace_min": "60",
    "enroll_coord": "AMPOTAKA-COORD",   # USSD/SMS enrollment codes; production seeds random ones
    "enroll_admin": "AMPOTAKA-ADMIN",
    "coord_access": "Coord@2026",  # asked by the web sign-up form; an administrator still approves every coordinator
}


def now_local():
    return datetime.now(TZ).replace(tzinfo=None)


def today_local():
    return now_local().date()


# ── settings ────────────────────────────────────────────────────────────────
def get_setting(key):
    row = db.session.get(Setting, key)
    return row.value if row else DEFAULT_SETTINGS.get(key, "")


def get_int(key):
    try:
        return int(get_setting(key))
    except (TypeError, ValueError):
        return int(DEFAULT_SETTINGS.get(key, "0") or 0)


def set_setting(key, value):
    row = db.session.get(Setting, key)
    if row:
        row.value = str(value)
    else:
        db.session.add(Setting(key=key, value=str(value)))


# ── formatting ──────────────────────────────────────────────────────────────
def fmt_ar(n):
    return f"{int(n or 0):,} MGA"


def fmt_min(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def parse_hhmm(s, default):
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", s or "")
    if not m:
        return default
    h, mi = int(m.group(1)), int(m.group(2))
    return h * 60 + mi if 0 <= h <= 24 and 0 <= mi < 60 else default


# ── validation ──────────────────────────────────────────────────────────────
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def valid_email(s):
    return bool(s) and len(s) <= 190 and bool(EMAIL_RE.match(s))


def password_error(pw):
    """Returns a translatable message or None. FR1.3 password-strength rules."""
    if len(pw or "") < 10:
        return "Password must be at least 10 characters."
    if not re.search(r"[a-z]", pw) or not re.search(r"[A-Z]", pw):
        return "Password needs both upper and lower case letters."
    if not re.search(r"\d", pw):
        return "Password needs at least one digit."
    if not re.search(r"[^A-Za-z0-9]", pw):
        return "Password needs at least one symbol or space."
    return None


def norm_phone(raw):
    """+261 34 12 345 67, 034 12 345 67 and 26134... all normalise to +261341234567."""
    s = re.sub(r"[^\d+]", "", raw or "")
    if not s:
        return ""
    if s.startswith("00"):
        s = "+" + s[2:]
    if s.startswith("0") and len(s) == 10:
        s = "+261" + s[1:]
    if not s.startswith("+"):
        s = "+" + s
    digits = s[1:]
    return s if digits.isdigit() and 9 <= len(digits) <= 15 else ""


def clean_text(s, limit=120):
    s = re.sub(r"[\x00-\x1f\x7f]", " ", s or "")
    return re.sub(r"\s+", " ", s).strip()[:limit]


# ── TOTP (RFC 6238) for optional MFA ────────────────────────────────────────
def new_totp_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret, counter, digits=6):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = mac[-1] & 0x0F
    return str((struct.unpack(">I", mac[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** digits).zfill(digits)


def totp_verify(secret, code, window=1):
    code = re.sub(r"\s", "", code or "")
    if not secret or not code.isdigit() or len(code) != 6:
        return False
    counter = int(time.time() // 30)
    return any(hmac.compare_digest(_hotp(secret, counter + d), code) for d in range(-window, window + 1))


def totp_uri(secret, email):
    return f"otpauth://totp/CWAS:{urllib.parse.quote(email)}?secret={secret}&issuer=CWAS&digits=6&period=30"


# ── audit trail (FR7.4, NFR7) ───────────────────────────────────────────────
def _actor_bits(actor):
    if actor is None and has_request_context():
        from flask_login import current_user
        actor = current_user if getattr(current_user, "is_authenticated", False) else None
    if actor is None:
        return None, "system"
    return actor.id, (actor.email or actor.phone or actor.name)


def audit(action, entity="", entity_id="", detail="", actor=None, channel=None):
    actor_id, label = _actor_bits(actor)
    ip = ""
    if has_request_context():
        ip = (request.headers.get("X-Forwarded-For", request.remote_addr) or "").split(",")[0].strip()[:64]
    last = AuditLog.query.order_by(AuditLog.id.desc()).first()
    at = utcnow()
    row = AuditLog(at=at, actor_id=actor_id, actor_label=label[:190], channel=channel or "web", action=action,
                   entity=entity, entity_id=str(entity_id), detail=clean_text(detail, 500), ip=ip,
                   prev_hash=last.hash if last else "GENESIS")
    row.hash = _chain_hash(row)
    db.session.add(row)
    db.session.flush()


def _chain_hash(r):
    raw = "|".join([r.prev_hash, r.at.isoformat(), str(r.actor_id or ""), r.action, r.entity, r.entity_id,
                    r.detail, r.channel])
    return hashlib.sha256(raw.encode()).hexdigest()


def verify_audit_chain():
    prev, count = "GENESIS", 0
    for r in AuditLog.query.order_by(AuditLog.id):
        if r.prev_hash != prev or _chain_hash(r) != r.hash:
            return False, r.id, count
        prev, count = r.hash, count + 1
    return True, None, count


