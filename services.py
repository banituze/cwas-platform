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


# ── people: names and phone numbers as households read them ─────────────────
log = logging.getLogger("cwas.services")
def ussd_code(raw):
    """The USSD code exactly as a caller dials it. It always ends with #: a .env line AT_USSD_CODE=*384*9411# read by a
    parser that starts a comment at # arrives as *384*9411, which phones reject as an invalid MMI code. Quotes and spaces
    around the value are dropped too."""
    code = (raw or "").strip().strip("'\"").replace(" ", "") or "*384*9411#"
    return code if code.endswith("#") else code + "#"


DIAL = ussd_code(os.environ.get("AT_USSD_CODE"))
RECOVERY_RE = re.compile(r"^\d{6}$")  # recovery code: six digits, distinct from the four-digit PIN


def first_name(name, limit=20):
    """First word of a name, for greetings: "Winebald Banituze" becomes "Winebald", never a name cut mid-word."""
    parts = (name or "").strip().split()
    return parts[0][:limit] if parts else ""


def local_phone(phone):
    """+261340000001 written the Malagasy way, 034 00 000 01. Other numbers are returned unchanged."""
    p = phone or ""
    if p.startswith("+261") and len(p) == 13 and p[1:].isdigit():
        d = "0" + p[4:]
        return f"{d[:3]} {d[3:5]} {d[5:8]} {d[8:]}"
    return p


# ── notifications and SMS ───────────────────────────────────────────────────
def notify(user, template, kind="system", body="", **params):
    if not user:
        return None
    n = Notification(user_id=user.id, kind=kind, key=template, params=json.dumps(params, default=str), body=body)
    db.session.add(n)
    return n


def render_notification(n, lang):
    if n.body and not n.key:
        return n.body
    try:
        params = json.loads(n.params or "{}")
    except ValueError:
        params = {}
    return tt(n.key, lang, **params)


OK_SMS = {"Success", "Sent", "Queued", "Processed"}


def _at_post(data, sandbox):
    """One call to the Africa's Talking messaging API. Returns the recipient status ("Success", "InvalidSenderId", ...)."""
    url = ("https://api.sandbox.africastalking.com" if sandbox else "https://api.africastalking.com") + "/version1/messaging"
    req = urllib.request.Request(url, urllib.parse.urlencode(data).encode(), {
        "apiKey": os.environ["AT_API_KEY"], "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:  # noqa: S310 - fixed https endpoint
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:  # some rejections come back as 4xx with the reason in the body
        raw = e.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - network trouble: the message stays recorded as failed
        return "failed"
    if "InvalidSenderId" in raw:
        return "InvalidSenderId"
    try:
        rec = (json.loads(raw).get("SMSMessageData") or {}).get("Recipients") or []
        return str(rec[0].get("status") or "failed") if rec else "failed"
    except (ValueError, AttributeError, IndexError, TypeError):
        return "failed"


def send_sms(phone, body, user=None, live=True, log_body=None):
    """Sends through Africa's Talking when SMS_ENABLED=1 and a key is set; otherwise records a simulated send.

    The sender is AT_SENDER_ID (for example CWAS), or AT_SHORTCODE. It is sent in the sandbox as well: Africa's Talking
    only shows its default AFRICASTKNG when no sender is given. If the sender is not registered on the account yet
    (InvalidSenderId), the message is sent again without it, so it still arrives. log_body replaces the stored text of a
    live send that carries a secret (a temporary PIN); in simulation the log is the delivery, so it keeps the real text."""
    if not phone:
        return None
    body = body[:640]
    row = SmsLog(direction="out", phone=phone, body=body, status="simulated")
    db.session.add(row)
    if live and os.environ.get("SMS_ENABLED", "0") == "1" and os.environ.get("AT_API_KEY"):
        username = os.environ.get("AT_USERNAME", "sandbox")
        sandbox = username == "sandbox"
        data = {"username": username, "to": phone, "message": body}
        sender = (os.environ.get("AT_SENDER_ID", "CWAS") or os.environ.get("AT_SHORTCODE") or "").strip()
        if sender:
            data["from"] = sender
        status = _at_post(data, sandbox)
        if status == "InvalidSenderId" and "from" in data:
            log.warning("Sender %r is not registered on this Africa's Talking app; sent with the default sender.", sender)
            del data["from"]
            status = _at_post(data, sandbox)
        row.status = "sent" if status in OK_SMS else "failed"
        if log_body:
            row.body = log_body[:640]
    return row


def sms_user(user, template, **params):
    if user and user.phone:
        send_sms(user.phone, tt(template, user.language, **params), user)


def event(user, template, kind="system", sms=False, **params):
    """One completed action: always an in-app notification, in the person's language. An SMS as well only when it
    matters (sms=True): an account created, a payment confirmed or refused, a booking approved or denied, a booking
    cancelled for maintenance, a coordinator message or a security alert. Routine changes stay in the app."""
    n = notify(user, template, kind, **params)
    if sms:
        sms_user(user, template, **params)
    return n


def payment_mode():
    """live keeps deposits pending until the provider confirms; simulation posts at once. Production defaults to live so
    nobody can mint money by accident."""
    prod = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("CWAS_ENV") == "production")
    return os.environ.get("PAYMENT_MODE") or ("live" if prod else "simulation")


# ── wallet ledger (FR2.4, FR5) ──────────────────────────────────────────────
class ServiceError(Exception):
    def __init__(self, code, **params):
        super().__init__(code)
        self.code = code
        self.params = params


_REF_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
PROVIDER_LABEL = {"orange": "Orange Money", "airtel": "Airtel Money", "cash": "Cash / agent"}


def new_ref(prefix):
    """CW-529E0CB1 for bookings, WT-A4E47E34 for wallet movements: eight hex characters, easy to read over SMS."""
    return f"{prefix}-{secrets.token_hex(4).upper()}"


def lock_household(hid):
    return db.session.query(Household).filter_by(id=hid).with_for_update().one()


def wallet_post(h, kind, amount, provider, note="", booking=None, status="posted", actor=None):
    """Adds one ledger row. Only posted rows move the spendable balance; pending mobile-money rows do not."""
    prefix = "WT"
    if status == "posted" and amount < 0 and h.balance + amount < 0:
        raise ServiceError("insufficient_funds", need=-amount, balance=h.balance)
    txn = WalletTxn(household_id=h.id, kind=kind, amount=amount, status=status, provider=provider,
                    reference=new_ref(prefix), booking_id=booking.id if booking else None, note=note[:255],
                    created_by=actor.id if actor else None)
    if status == "posted":
        h.balance += amount
        txn.balance_after = h.balance
    db.session.add(txn)
    db.session.flush()
    return txn


def deposit(h, amount, provider, actor=None, channel="web"):
    """Simulation mode posts instantly. Live mode records a pending row until a provider adapter confirms it."""
    lo, hi = get_int("min_deposit"), get_int("max_deposit")
    if not isinstance(amount, int) or amount < lo or amount > hi:
        raise ServiceError("amount_range", lo=fmt_ar(lo), hi=fmt_ar(hi))
    if provider not in ("orange", "airtel", "cash"):
        raise ServiceError("provider_invalid")
    if provider == "cash" and get_setting("cash_enabled") != "1":
        raise ServiceError("provider_invalid")
    h = lock_household(h.id)
    status = "pending" if payment_mode() == "live" and channel != "staff" else "posted"
    txn = wallet_post(h, "deposit", amount, provider, note=f"{channel} deposit", status=status, actor=actor)
    audit("wallet.deposit", "wallet", txn.reference, f"{provider} {amount} MGA {status}", actor=actor, channel=channel)
    label = PROVIDER_LABEL.get(provider, provider)
    if status == "posted":
        event(h.user, "Deposit {ref} recorded: +{amount} via {provider}. Your balance is {balance}.", "wallet", sms=True, ref=txn.reference,
              amount=fmt_ar(amount), provider=label, balance=fmt_ar(h.balance))
    else:
        event(h.user, "Deposit {ref} of {amount} via {provider} is waiting for confirmation.", "wallet", ref=txn.reference,
              amount=fmt_ar(amount), provider=label)
    return txn


def confirm_pending_deposit(txn, actor=None, channel="web"):
    if txn.status != "pending" or txn.kind != "deposit":
        raise ServiceError("not_pending")
    h = lock_household(txn.household_id)
    txn.status = "posted"
    h.balance += txn.amount
    txn.balance_after = h.balance
    audit("wallet.confirm", "wallet", txn.reference, f"{txn.provider} {txn.amount} MGA confirmed", actor=actor, channel=channel)
    event(h.user, "Your deposit {ref} of {amount} was confirmed. Your new balance is {balance}.", "wallet", sms=True,
          ref=txn.reference, amount=fmt_ar(txn.amount), balance=fmt_ar(h.balance))
    return txn


def reconcile_wallet(h):
    posted = db.session.query(func.coalesce(func.sum(WalletTxn.amount), 0)).filter(
        WalletTxn.household_id == h.id, WalletTxn.status == "posted").scalar()
    return int(posted) == h.balance


def reject_pending_deposit(txn, actor=None, channel="web", reason=""):
    """A coordinator could not match a pending mobile-money deposit to a real payment. Nothing reaches the wallet and
    the household is told at once, by SMS too, so they can follow up."""
    if txn.status != "pending" or txn.kind != "deposit":
        raise ServiceError("not_pending")
    txn.status = "failed"
    h = db.session.get(Household, txn.household_id)
    audit("wallet.reject", "wallet", txn.reference, clean_text(reason, 120), actor=actor, channel=channel)
    event(h.user, "Your deposit {ref} of {amount} was not confirmed, so nothing was added. Ask your coordinator if you paid.",
          "wallet", sms=True, ref=txn.reference, amount=fmt_ar(txn.amount))
    return txn


# ── PIN and recovery code (USSD and SMS credentials) ────────────────────────
_WEAK_PINS = {"0000", "1111", "2222", "9999", "1234", "4321", "0123", "1212"}


def set_pin(user, pin, actor=None, channel="web"):
    """A new PIN. Clears failed attempts and sends a security SMS, so a change nobody asked for is noticed."""
    user.pin_hash, user.pin_failed, user.pin_locked_until = generate_password_hash(pin), 0, None
    audit("user.pin_change", "user", user.id, channel, actor=actor or user, channel=channel)
    event(user, "Your PIN was changed. If this was not you, contact your coordinator.", "system", sms=True)


def set_recovery(user, code, actor=None, channel="web"):
    """A new recovery code. It resets a forgotten PIN without calling anyone, so it is stored hashed like the PIN."""
    user.recovery_hash = generate_password_hash(code)
    audit("user.recovery_set", "user", user.id, channel, actor=actor or user, channel=channel)
    event(user, "Your recovery code was changed. If this was not you, contact your coordinator.", "system", sms=True)


def reset_pin_by_staff(target, actor, channel="web"):
    """A coordinator resets a household PIN after checking the person's details by phone. The temporary PIN goes only
    to the household's own phone; the coordinator never sees it, and a live send does not keep it in the SMS log."""
    pin = "0000"
    while pin in _WEAK_PINS:
        pin = f"{secrets.randbelow(10000):04d}"
    target.pin_hash, target.pin_failed, target.pin_locked_until = generate_password_hash(pin), 0, None
    audit("user.pin_reset", "user", target.id, f"by {actor.role} #{actor.id}", actor=actor, channel=channel)
    notify(target, "Your PIN was reset by a coordinator. Change the temporary PIN in My profile.", "system")
    msg = "CWAS: your coordinator reset your PIN. Temporary PIN: {pin}. Dial {dial} and change it in My profile."
    send_sms(target.phone, tt(msg, target.language, pin=pin, dial=DIAL), target, log_body=tt(msg, target.language, pin="****", dial=DIAL))
    return True


def staff_contacts(user=None, limit=2):
    """Numbers a person can call for help: coordinators (then administrators) for households, administrators for staff."""
    from models import User
    roles = ("admin",) if user is not None and user.role in ("coordinator", "admin") else ("coordinator", "admin")
    q = User.query.filter(User.role.in_(roles), User.is_active_flag.is_(True), User.phone.isnot(None))
    if user is not None:
        q = q.filter(User.id != user.id)
    rows = sorted(q.all(), key=lambda u: (roles.index(u.role), u.id))
    return [u.phone for u in rows[:limit]]


PIN_HELP_ACK = "You asked for help with your PIN. A coordinator will call you to check your details."
PIN_HELP_STAFF = "PIN help: {who} forgot the PIN and has no recovery code. Call to check their details, then reset the PIN."


def request_pin_help(user, channel="sms"):
    """PIN HELP: forgot the PIN and has no recovery code. Coordinators (administrators, for staff) get the request in
    the app and by SMS, call back, check the person's details and reset the PIN. One request an hour, so a repeated
    text never floods their phones. Returns False when a request is already open."""
    from models import User
    if Notification.query.filter(Notification.user_id == user.id, Notification.key == PIN_HELP_ACK,
                                 Notification.created_at >= utcnow() - timedelta(hours=1)).first():
        return False
    roles = ("admin",) if user.role in ("coordinator", "admin") else ("coordinator", "admin")
    h = user.household
    who = f"{user.name} ({local_phone(user.phone)})" + (f", {h.village}" if h and h.village else "")
    for st in User.query.filter(User.role.in_(roles), User.is_active_flag.is_(True), User.id != user.id).all():
        notify(st, PIN_HELP_STAFF, "system", who=who)
        if st.phone and st.role == roles[0]:
            send_sms(st.phone, tt(PIN_HELP_STAFF, st.language, who=who), st)
    notify(user, PIN_HELP_ACK, "system")
    audit("user.pin_help", "user", user.id, channel, actor=user, channel=channel)
    return True


# ── slots (FR3.2, FR4.1, FR9.2) ─────────────────────────────────────────────
def price_quote(h, source, litres):
    base = round(source.tariff_per_100l * litres / 100)
    pct = {"standard": 0, "elevated": get_int("discount_elevated"), "high": get_int("discount_high")}.get(h.priority_level, 0)
    return round(base * (100 - pct) / 100), pct, base


def litre_options(source):
    return [l for l in LITRE_STEPS if l <= source.max_litres] or [source.max_litres]


def slot_list(source, day, only_open=False):
    if source.status != "operational":
        return []
    now = now_local()
    counts = dict(db.session.query(Booking.start_min, func.count(Booking.id)).filter(
        Booking.source_id == source.id, Booking.date == day, Booking.status.in_(Booking.ACTIVE)
    ).group_by(Booking.start_min).all())
    day_total = sum(counts.values())
    d0 = datetime.combine(day, datetime.min.time())
    windows = Maintenance.query.filter(Maintenance.source_id == source.id, Maintenance.status == "scheduled",
                                       Maintenance.starts_at < d0 + timedelta(days=1), Maintenance.ends_at > d0).all()
    out, m = [], source.open_min
    step = max(5, source.slot_minutes)
    while m + step <= source.close_min:
        s_dt, e_dt = d0 + timedelta(minutes=m), d0 + timedelta(minutes=m + step)
        used = counts.get(m, 0)
        if e_dt <= now:
            m += step
            continue
        if any(w.starts_at < e_dt and w.ends_at > s_dt for w in windows):
            state = "blocked"
        elif used >= source.slot_capacity or day_total >= source.daily_capacity:
            state = "full"
        else:
            state = "open"
        if not only_open or state == "open":
            out.append({"start_min": m, "end_min": m + step, "label": f"{fmt_min(m)}-{fmt_min(m + step)}",
                        "used": used, "capacity": source.slot_capacity, "free": max(0, source.slot_capacity - used),
                        "state": state})
        m += step
    return out


def booking_days():
    t = today_local()
    return [t + timedelta(days=i) for i in range(HORIZON_DAYS)]


def operational_sources():
    return WaterSource.query.filter_by(status="operational").order_by(WaterSource.name).all()


def alternatives(source, day, litres, limit=3):
    """FR11.5 conflict resolution: nearest open slots, same source first, then same time elsewhere."""
    found = []
    for d in booking_days():
        if d < day:
            continue
        for s in slot_list(source, d, only_open=True):
            found.append((abs((d - day).days) * 1440 + abs(s["start_min"] - 720), source, d, s))
    for other in operational_sources():
        if other.id == source.id:
            continue
        for s in slot_list(other, day, only_open=True)[:2]:
            found.append((2000 + abs(s["start_min"] - 720), other, day, s))
    found.sort(key=lambda x: x[0])
    return [{"source": f[1], "date": f[2], "slot": f[3]} for f in found[:limit]]


def alt_text(alts, lang):
    return "; ".join(f"{a['source'].name} {a['date']:%d/%m} {a['slot']['label']}" for a in alts) or tt("no free slot this week", lang)


# ── AI: explainable priority, risk and forecasting (FR11) ───────────────────
# ── household needs: the same questions, in the same order, on the web, by USSD and when a coordinator registers ──
VULN = [("elderly", "Someone aged 60 or over", "Aged 60+"), ("disability", "Someone living with a disability", "Disability"),
        ("infant", "A child under 5", "Child under 5"), ("pregnant", "Pregnant or breastfeeding", "Pregnant/nursing"),
        ("single", "Single-parent household", "Single parent"), ("illness", "Long-term illness", "Long illness")]
VULN_CODES = [c for c, _, _ in VULN]
DISTANCE = [(100, "Under 200 m"), (350, "200 m to 500 m"), (750, "500 m to 1 km"), (1500, "1 km to 2 km"), (3500, "2 km to 5 km"), (6000, "Over 5 km")]
LEVEL_POINTS = {"standard": 0, "elevated": 25, "high": 45}


def clean_flags(values):
    """Known codes only, in a fixed order, so the same answers always store the same text."""
    got = {str(v).strip() for v in values or []}
    return ",".join(c for c in VULN_CODES if c in got)


def reported_level(flags):
    """What the household told us, as a level: one need is elevated, two or more is high. A coordinator confirms it."""
    n = len([c for c in (flags or "").split(",") if c in VULN_CODES])
    return "high" if n >= 2 else "elevated" if n == 1 else "standard"


def distance_label(m):
    return min(DISTANCE, key=lambda d: abs(d[0] - (m or 0)))[1]


OTHER_SOURCE = "other:"  # prefix of a water point the household names itself (not in the list yet)


def set_needs(h, flags, distance_m=None, source_id=None):
    """Store the household's own answers. Priority uses them at once; the subsidy waits for a coordinator's check.
    source_id is a water point id, 0/None for Not sure, or "other:<name>" for a point that is not in the list."""
    h.vuln_flags = clean_flags(flags.split(",") if isinstance(flags, str) else flags)
    if distance_m is not None:
        h.distance_m = distance_m
    if isinstance(source_id, str) and source_id.startswith(OTHER_SOURCE):
        h.home_source_id, h.home_source_note = None, clean_text(source_id[len(OTHER_SOURCE):], 80)
    elif source_id is not None:
        h.home_source_id, h.home_source_note = (source_id or None), ""
    h.needs_review = bool(h.vuln_flags) and LEVEL_POINTS[reported_level(h.vuln_flags)] > LEVEL_POINTS.get(h.priority_level, 0)


def priority_breakdown(h):
    """Every point of a priority score has a stated reason (NFR11: explainable, checked for bias)."""
    lvl = h.priority_level
    if h.needs_review and LEVEL_POINTS[reported_level(h.vuln_flags)] > LEVEL_POINTS.get(lvl, 0):
        vuln = ("Vulnerability level (self-reported, awaiting check)", LEVEL_POINTS[reported_level(h.vuln_flags)])
    else:
        vuln = ("Vulnerability level", LEVEL_POINTS.get(lvl, 0))
    parts = [vuln,
             ("Household size", min(h.family_size, 12) * 3),
             ("Distance to water", min(h.distance_m // 200, 10))]
    since = today_local() - timedelta(days=7)
    recent = Booking.query.filter(Booking.household_id == h.id, Booking.date >= since,
                                  Booking.status.in_(("approved", "collected", "pending"))).count()
    parts.append(("Fair-share boost (few recent bookings)", max(0, 10 - recent * 3)))
    since14 = today_local() - timedelta(days=14)
    ns = Booking.query.filter(Booking.household_id == h.id, Booking.date >= since14, Booking.status == "no_show").count()
    parts.append(("Recent no-shows", -min(ns * 5, 15)))
    total = max(0, min(100, sum(p for _, p in parts)))
    return total, parts


def assess_booking(h, litres, day):
    """Returns (score, note, suggestion). The suggestion never overrides a coordinator."""
    score, _ = priority_breakdown(h)
    need = max(40, h.family_size * 20)
    notes, suggestion = [], "approve"
    if litres > need * 1.5:
        notes.append("Litres above the household's usual need")
        suggestion = "review"
    since14 = today_local() - timedelta(days=14)
    if Booking.query.filter(Booking.household_id == h.id, Booking.date >= since14, Booking.status == "no_show").count() >= 2:
        notes.append("Repeated no-shows")
        suggestion = "review"
    if Booking.query.filter(Booking.household_id == h.id, Booking.status == "cancelled",
                            Booking.created_at >= utcnow() - timedelta(days=7)).count() >= 3:
        notes.append("Frequent cancellations")
        suggestion = "review"
    return score, "; ".join(notes) or "Within normal pattern", suggestion


def forecast(source, weeks=8):
    """Weekday seasonality from recent history. Falls back to a modest baseline when history is thin."""
    t = today_local()
    since = t - timedelta(weeks=weeks * 7)
    rows = db.session.query(Booking.date, func.count(Booking.id)).filter(
        Booking.source_id == source.id, Booking.date >= since, Booking.date < t,
        Booking.status.in_(("approved", "collected", "no_show", "pending"))).group_by(Booking.date).all()
    by_wd = {i: [] for i in range(7)}
    for d, c in rows:
        by_wd[d.weekday()].append(c)
    all_counts = [c for _, c in rows]
    fallback = statistics.mean(all_counts) if all_counts else source.daily_capacity * 0.3
    out = []
    for i in range(HORIZON_DAYS):
        d = t + timedelta(days=i)
        vals = by_wd[d.weekday()]
        exp = round(statistics.mean(vals) if vals else fallback)
        ratio = exp / max(1, source.daily_capacity)
        out.append({"date": d, "expected": exp, "ratio": ratio,
                    "level": "high" if ratio >= 0.8 else "medium" if ratio >= 0.5 else "low", "samples": len(vals)})
    hours = db.session.query(Booking.start_min, func.count(Booking.id)).filter(
        Booking.source_id == source.id, Booking.date >= since, Booking.status.in_(("approved", "collected", "pending", "no_show"))
    ).group_by(Booking.start_min).order_by(func.count(Booking.id).desc()).first()
    return out, (fmt_min(hours[0]) if hours else None)


def detect_anomalies():
    """FR11.4 - flags patterns for review. Rules are transparent thresholds, not black-box scores."""
    flags, t = [], utcnow()
    week = t - timedelta(days=7)
    canc = db.session.query(Booking.household_id, func.count(Booking.id)).filter(
        Booking.status == "cancelled", Booking.created_at >= week).group_by(Booking.household_id).having(func.count(Booking.id) >= 3).all()
    for hid, c in canc:
        h = db.session.get(Household, hid)
        flags.append({"severity": "medium", "who": h.name, "text": "{n} cancellations in 7 days", "params": {"n": c}})
    ns = db.session.query(Booking.household_id, func.count(Booking.id)).filter(
        Booking.status == "no_show", Booking.date >= today_local() - timedelta(days=14)).group_by(Booking.household_id).having(func.count(Booking.id) >= 3).all()
    for hid, c in ns:
        h = db.session.get(Household, hid)
        flags.append({"severity": "medium", "who": h.name, "text": "{n} no-shows in 14 days", "params": {"n": c}})
    dep = db.session.query(WalletTxn.household_id, func.count(WalletTxn.id)).filter(
        WalletTxn.kind == "deposit", WalletTxn.created_at >= t - timedelta(hours=1)).group_by(WalletTxn.household_id).having(func.count(WalletTxn.id) >= 3).all()
    for hid, c in dep:
        h = db.session.get(Household, hid)
        flags.append({"severity": "high", "who": h.name, "text": "{n} deposits within one hour", "params": {"n": c}})
    big = WalletTxn.query.filter(WalletTxn.kind == "deposit", WalletTxn.amount >= 50000, WalletTxn.created_at >= week).all()
    for x in big:
        flags.append({"severity": "medium", "who": x.household.name, "text": "Large deposit {amount}", "params": {"amount": fmt_ar(x.amount)}})
    lit = [b.litres for b in Booking.query.filter(Booking.created_at >= t - timedelta(days=30)).all()]
    if len(lit) >= 10:
        mu, sd = statistics.mean(lit), statistics.pstdev(lit) or 1
        for b in Booking.query.filter(Booking.created_at >= week, Booking.litres > mu + 2 * sd).limit(5):
            flags.append({"severity": "low", "who": b.household.name, "text": "Unusually large request: {l} L", "params": {"l": b.litres}})
    return flags


def fairness_check():
    """NFR11 - compares approval rates across vulnerability levels so the system does not repeat unfairness."""
    rows = []
    for level in ("standard", "elevated", "high"):
        q = Booking.query.join(Household).filter(Household.priority_level == level, Booking.status.in_(("approved", "collected", "denied", "no_show")))
        total = q.count()
        ok = q.filter(Booking.status.in_(("approved", "collected", "no_show"))).count()
        hh = Household.query.filter_by(priority_level=level).count()
        rows.append({"level": level, "households": hh, "decided": total, "approval": round(100 * ok / total) if total else None})
    rates = [r["approval"] for r in rows if r["approval"] is not None]
    flagged = len(rates) >= 2 and (max(rates) - min(rates)) > 25
    return rows, flagged


# ── bookings (FR4, FR5.1, FR6) ──────────────────────────────────────────────
def create_booking(h, source_id, day, start_min, litres, channel="web", actor=None):
    """Final confirmation for a booking. Rechecks everything, then debits the wallet and creates the linked booking
    in one transaction. Nothing is created when any check fails."""
    src = db.session.get(WaterSource, source_id)
    if not src or src.status != "operational":
        raise ServiceError("source_unavailable")
    t = today_local()
    if day < t or day >= t + timedelta(days=HORIZON_DAYS):
        raise ServiceError("date_range")
    if litres not in litre_options(src):
        raise ServiceError("litres_invalid")
    slot = next((s for s in slot_list(src, day) if s["start_min"] == start_min), None)
    if not slot:
        raise ServiceError("slot_unavailable")
    if slot["state"] == "blocked":
        raise ServiceError("slot_blocked", alts=alternatives(src, day, litres))
    if slot["state"] == "full":
        raise ServiceError("slot_full", alts=alternatives(src, day, litres))
    h = lock_household(h.id)
    if Booking.query.filter(Booking.household_id == h.id, Booking.date == day, Booking.status.in_(Booking.ACTIVE)).first():
        raise ServiceError("one_per_day")
    amount, pct, _ = price_quote(h, src, litres)
    if h.balance < amount:
        raise ServiceError("insufficient_funds", need=amount, balance=h.balance)
    score, note, suggestion = assess_booking(h, litres, day)
    ref = new_ref("CW")
    b = Booking(ref=ref, household_id=h.id, source_id=src.id, date=day, start_min=slot["start_min"], end_min=slot["end_min"],
                litres=litres, amount=amount, discount_pct=pct, status="pending", channel=channel, priority_score=score,
                ai_note=note[:255], ai_suggestion=suggestion)
    db.session.add(b)
    db.session.flush()
    wallet_post(h, "booking_debit", -amount, "wallet", note=f"Booking {ref}", booking=b, actor=actor)
    audit("booking.create", "booking", ref, f"{src.name} {day} {fmt_min(b.start_min)} {litres}L {amount} MGA", actor=actor, channel=channel)
    if get_setting("auto_approve") == "1" and suggestion == "approve":
        b.status, b.decided_at, b.decision_note = "approved", utcnow(), "Auto-approved (low risk)"
        audit("booking.auto_approve", "booking", ref, "AI low-risk auto approval", channel="system")
        event(h.user, "Booking {ref} is approved: {source}, {date} {time}.", "booking", sms=True, ref=ref, source=src.name,
              date=f"{day:%d/%m}", time=fmt_min(b.start_min))
    else:
        event(h.user, "Booked {ref}: {source} {date} {time}, {litres} L, {amount}. Status: pending approval.", "booking",
              ref=ref, source=src.name, date=f"{day:%d/%m}", time=fmt_min(b.start_min), litres=litres, amount=fmt_ar(amount))
    return b


def _refund(b, reason, actor=None, channel="web"):
    h = lock_household(b.household_id)
    debit = WalletTxn.query.filter_by(booking_id=b.id, kind="booking_debit", status="posted").first()
    already = WalletTxn.query.filter_by(booking_id=b.id, kind="booking_refund", status="posted").first()
    if debit and not already:
        wallet_post(h, "booking_refund", -debit.amount, "wallet", note=reason, booking=b, actor=actor)
        return -debit.amount
    return 0


def cancel_booking(b, actor=None, channel="web"):
    start = datetime.combine(b.date, datetime.min.time()) + timedelta(minutes=b.start_min)
    if b.status not in ("pending", "approved"):
        raise ServiceError("not_cancellable")
    if start <= now_local():
        raise ServiceError("too_late")
    b.status = "cancelled"
    amount = _refund(b, f"Refund for cancelled {b.ref}", actor, channel)
    audit("booking.cancel", "booking", b.ref, f"refund {amount}", actor=actor, channel=channel)
    event(b.household.user, "Booking {ref} was cancelled. {amount} returned to your wallet.", "booking", ref=b.ref, amount=fmt_ar(amount))
    return b


def decide_booking(b, approve, note="", actor=None, channel="web"):
    if b.status != "pending":
        raise ServiceError("not_pending")
    b.decided_by, b.decided_at, b.decision_note = (actor.id if actor else None), utcnow(), clean_text(note, 250)
    if approve:
        b.status = "approved"
        audit("booking.approve", "booking", b.ref, note, actor=actor, channel=channel)
        event(b.household.user, "Booking {ref} is approved: {source}, {date} {time}.", "booking", sms=True, ref=b.ref,
              source=b.source.name, date=f"{b.date:%d/%m}", time=fmt_min(b.start_min))
    else:
        b.status = "denied"
        amount = _refund(b, f"Refund for denied {b.ref}", actor, channel)
        audit("booking.deny", "booking", b.ref, note, actor=actor, channel=channel)
        event(b.household.user, "Booking {ref} was not approved. {amount} returned to your wallet. Reason: {reason}", "booking", sms=True,
              ref=b.ref, amount=fmt_ar(amount), reason=b.decision_note or "-")
    return b


def mark_collected(b, actor=None, channel="web"):
    if b.status != "approved":
        raise ServiceError("not_approved")
    b.status = "collected"
    audit("booking.collected", "booking", b.ref, "", actor=actor, channel=channel)
    event(b.household.user, "Water collected for booking {ref}. Thank you!", "booking", ref=b.ref)
    return b


def mark_no_show(b, actor=None, channel="web"):
    if b.status != "approved":
        raise ServiceError("not_approved")
    b.status = "no_show"
    audit("booking.no_show", "booking", b.ref, "", actor=actor, channel=channel)
    event(b.household.user, "Booking {ref} was marked as not collected.", "booking", ref=b.ref)
    return b


def sweep(now=None):
    """Housekeeping: unreviewed requests whose slot has passed are cancelled and refunded; approved slots left
    uncollected an hour after closing become no-shows."""
    now = now or now_local()
    grace = get_int("no_show_grace_min")
    changed = 0
    for b in Booking.query.filter(Booking.status.in_(("pending", "approved")), Booking.date <= now.date()).all():
        end = datetime.combine(b.date, datetime.min.time()) + timedelta(minutes=b.end_min)
        if b.status == "pending" and end <= now:
            b.status = "cancelled"
            amount = _refund(b, f"Refund for expired {b.ref}", None, "system")
            audit("booking.expire", "booking", b.ref, "unreviewed until slot end", channel="system")
            notify(b.household.user, "Booking {ref} expired before review. {amount} returned to your wallet.", "booking", ref=b.ref, amount=fmt_ar(amount))
            changed += 1
        elif b.status == "approved" and end + timedelta(minutes=grace) <= now:
            b.status = "no_show"
            audit("booking.no_show", "booking", b.ref, "auto", channel="system")
            changed += 1
    if changed:
        db.session.commit()
    return changed


# ── maintenance (FR9) ───────────────────────────────────────────────────────
def schedule_maintenance(source, start, end, reason, actor=None, channel="web"):
    """Blocks the affected slots, cancels and refunds bookings inside the window, and offers alternatives."""
    if end <= start:
        raise ServiceError("bad_window")
    m = Maintenance(source_id=source.id, starts_at=start, ends_at=end, reason=clean_text(reason, 250), created_by=actor.id if actor else None)
    db.session.add(m)
    db.session.flush()
    affected = 0
    q = Booking.query.filter(Booking.source_id == source.id, Booking.status.in_(("pending", "approved")),
                             Booking.date >= start.date(), Booking.date <= end.date()).all()
    for b in q:
        s = datetime.combine(b.date, datetime.min.time()) + timedelta(minutes=b.start_min)
        e = datetime.combine(b.date, datetime.min.time()) + timedelta(minutes=b.end_min)
        if s < end and e > start:
            b.status = "cancelled"
            amount = _refund(b, f"Refund: maintenance at {source.name}", actor, channel)
            alts = alternatives(source, b.date, b.litres)
            for_user = b.household.user
            event(for_user, "Maintenance at {source}: booking {ref} was cancelled and {amount} refunded. Free slots: {alts}", "maintenance", sms=True,
                  source=source.name, ref=b.ref, amount=fmt_ar(amount), alts=alt_text(alts, for_user.language))
            affected += 1
    audit("maintenance.schedule", "source", source.id, f"{start} to {end}: {reason} ({affected} bookings moved)", actor=actor, channel=channel)
    return m, affected


# ── reports (FR7) ───────────────────────────────────────────────────────────
def _bookings_between(d0, d1, source_id=None):
    q = Booking.query.filter(Booking.date >= d0, Booking.date <= d1)
    return q.filter(Booking.source_id == source_id) if source_id else q


def usage_report(d0, d1, source_id=None):
    rows = _bookings_between(d0, d1, source_id).all()
    by_status, by_source, by_day, by_hour = {}, {}, {}, {}
    litres = 0
    for b in rows:
        by_status[b.status] = by_status.get(b.status, 0) + 1
        if b.status in ("approved", "collected", "no_show"):
            litres += b.litres
            by_source.setdefault(b.source.name, {"bookings": 0, "litres": 0})
            by_source[b.source.name]["bookings"] += 1
            by_source[b.source.name]["litres"] += b.litres
            by_day[b.date] = by_day.get(b.date, 0) + 1
            by_hour[b.start_min // 60] = by_hour.get(b.start_min // 60, 0) + 1
    return {"total": len(rows), "by_status": by_status, "by_source": by_source, "litres": litres,
            "by_day": sorted(by_day.items()), "by_hour": sorted(by_hour.items())}


def equity_report(d0, d1):
    out = []
    for level in ("standard", "elevated", "high"):
        bs = Booking.query.join(Household).filter(Household.priority_level == level, Booking.date >= d0, Booking.date <= d1,
                                                  Booking.status.in_(("approved", "collected", "no_show"))).all()
        hh = Household.query.filter_by(priority_level=level).all()
        people = sum(x.family_size for x in hh)
        litres = sum(b.litres for b in bs)
        days = max(1, (d1 - d0).days + 1)
        out.append({"level": level, "households": len(hh), "people": people, "bookings": len(bs), "litres": litres,
                    "lpcd": round(litres / people / days, 1) if people else 0})
    return out


def financial_report(d0, d1):
    a, b_ = datetime.combine(d0, datetime.min.time()), datetime.combine(d1 + timedelta(days=1), datetime.min.time())
    txns = WalletTxn.query.filter(WalletTxn.created_at >= a - timedelta(hours=3), WalletTxn.created_at < b_ - timedelta(hours=3)).all()
    dep, by_provider = 0, {}
    for x in txns:
        if x.kind == "deposit" and x.status == "posted":
            dep += x.amount
            by_provider[x.provider] = by_provider.get(x.provider, 0) + x.amount
    pending = sum(x.amount for x in txns if x.kind == "deposit" and x.status == "pending")
    refunds = sum(x.amount for x in txns if x.kind == "booking_refund" and x.status == "posted")
    bs = _bookings_between(d0, d1).filter(Booking.status.in_(("approved", "collected", "no_show"))).all()
    revenue = sum(x.amount for x in bs)
    by_source = {}
    for x in bs:
        by_source[x.source.name] = by_source.get(x.source.name, 0) + x.amount
    subsidy = sum(round(x.amount * x.discount_pct / max(1, 100 - x.discount_pct)) for x in bs if x.discount_pct)
    return {"deposits": dep, "pending": pending, "refunds": refunds, "revenue": revenue, "by_provider": by_provider,
            "by_source": by_source, "subsidy": subsidy, "held": db.session.query(func.coalesce(func.sum(Household.balance), 0)).scalar()}


def to_csv(header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        # Guard against spreadsheet formula injection in exported text.
        w.writerow([("'" + c) if isinstance(c, str) and c[:1] in "=+-@" else c for c in r])
    return buf.getvalue()


# ── assistant (FR11.3): keyword intents answered from live data ─────────────
_INTENTS = [
    ("balance", ("balance", "wallet", "solde", "vola", "sisa", "money left")),
    ("bookings", ("my booking", "next booking", "réservation", "famandrihana", "reservation", "bookings")),
    ("book", ("book", "reserve", "slot", "réserver", "mamandrika", "famandrihana vaovao")),
    ("cancel", ("cancel", "annul", "foano", "foana")),
    ("price", ("price", "cost", "tarif", "prix", "vidiny", "sarany", "how much")),
    ("hours", ("hour", "open", "horaire", "ouvert", "ora", "misokatra")),
    ("sources", ("water point", "source", "borehole", "puits", "fantsakana", "forage", "where")),
    ("ussd", ("ussd", "sms", "feature phone", "*384", "code")),
    ("deposit", ("deposit", "add money", "orange", "airtel", "pay", "recharge", "dépôt", "manampy", "mandoa")),
    ("language", ("language", "langue", "fiteny", "malagasy", "français", "english")),
    ("help", ("help", "aide", "fanampiana", "hello", "bonjour", "salama", "hi")),
]


def assistant_reply(user, text, lang):
    t = (text or "").lower()
    intent = next((name for name, keys in _INTENTS if any(k in t for k in keys)), "fallback")
    h = user.household if user and user.role == "member" else None
    if intent == "balance" and h:
        return tt("Your wallet balance is {balance}.", lang, balance=fmt_ar(h.balance))
    if intent == "bookings" and h:
        nxt = Booking.query.filter(Booking.household_id == h.id, Booking.date >= today_local(),
                                   Booking.status.in_(("pending", "approved"))).order_by(Booking.date, Booking.start_min).first()
        if nxt:
            return tt("Next booking {ref}: {source}, {date} {time}, {litres} L ({status}).", lang, ref=nxt.ref, source=nxt.source.name,
                      date=f"{nxt.date:%d/%m}", time=fmt_min(nxt.start_min), litres=nxt.litres, status=tt(nxt.status.replace("_", " "), lang))
        return tt("You have no upcoming booking.", lang)
    if intent == "sources":
        names = ", ".join(s.name for s in operational_sources()[:6]) or "-"
        return tt("Open water points: {names}.", lang, names=names)
    if intent == "price":
        s = operational_sources()
        if s:
            return tt("Tariff is about {p} per 100 L, reduced for vulnerable households.", lang, p=fmt_ar(s[0].tariff_per_100l))
    if intent == "hours":
        s = operational_sources()
        if s:
            return tt("{name} is open {a} to {b}.", lang, name=s[0].name, a=fmt_min(s[0].open_min), b=fmt_min(s[0].close_min))
    if intent in ("book", "cancel", "deposit"):
        return tt({"book": "Open Book a slot, choose a water point, a day and a time. Payment comes from your wallet.",
                   "cancel": "Open My bookings, pick a future booking and press Cancel. Your money returns to the wallet.",
                   "deposit": "Open Wallet and add money with Orange Money or Airtel Money."}[intent], lang)
    if intent == "ussd":
        return tt("Dial *384*9411# from any phone. SMS shortcode 7380 answers HELP, BALANCE, SOURCES and more.", lang)
    if intent == "language":
        return tt("Change the language from the flag menu at the top of the page.", lang)
    return tt("I can help with balance, bookings, prices, water points and USSD. What do you need?", lang)
