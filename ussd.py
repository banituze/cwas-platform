"""USSD and SMS channels (Africa's Talking compatible).

Africa's Talking POSTs sessionId, serviceCode, phoneNumber, networkCode and text on every key press. `text` holds every
entry of the session joined with '*', so a session is a pure function of `text`: each flow is a generator that yields
the screen to show and receives the next entry, and every hop replays the entries from the start. Nothing is written
until a final confirmation, and final steps end the session, so a replayed hop can never post twice. A repeated
identical (sessionId, text) returns the stored answer.

Every session starts with the language menu. Unknown phones register (household member, or coordinator/admin with an
enrollment code from system settings); registered phones enter a 4-digit PIN.
Navigation everywhere: 97 back, 00 home, 99 exit. Every screen fits in 182 characters.
"""
import hashlib
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta

from flask import current_app
from werkzeug.security import check_password_hash, generate_password_hash

from i18n import tt
from models import Booking, Household, Notification, SmsLog, UssdSession, User, WalletTxn, WaterSource, db, utcnow
import services as S

log = logging.getLogger("cwas.ussd")
MAX_SCREEN = 182
PIN_RE = re.compile(r"^\d{4}$")
DIAL = S.DIAL  # normalised once in services.ussd_code (always ends with #)
SHORTCODE = os.environ.get("AT_SHORTCODE") or "7380"
LANG_CODES = {"1": "mg", "2": "en", "3": "fr"}
LANG_NAMES = ["Malagasy", "English", "Francais"]
DENY_REASONS = ["Slot unavailable", "Maintenance",
                "Incomplete request", "Other, coordinator will call"]
_ENROLL_FAILS = {}
_ENROLL_LOCK = threading.Lock()


class Ctx:
    def __init__(self, lang="mg", user=None, phone="", channel="telco"):
        self.lang, self.user, self.phone, self.channel = lang, user, phone, channel
        self.rejected, self.secret, self.flash, self.trail = False, False, "", []
        self.consumed, self.home_len = 0, 0
        # PIN checked this session; account erased (log without the phone)
        self.pin_ok, self.erased = False, False

    def L(self, s, **kw):
        return tt(s, self.lang, **kw)


# ── rendering ───────────────────────────────────────────────────────────────
def _is_nav(line):
    return line[:3] in ("97.", "98.", "99.", "00.")


BLANK = " "  # a deliberate empty line between blocks; the first thing a crowded screen gives up


def _fit(lines):
    lines = [l for l in lines if l]
    if len("\n".join(lines)) > MAX_SCREEN:
        lines = [l for l in lines if l != BLANK]
    lines = ["" if l == BLANK else l for l in lines]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    text = "\n".join(lines)
    limit = 26
    while len(text) > MAX_SCREEN and limit > 8:
        lines = [(l if len(l) <= limit or i == 0 or _is_nav(
            l) else l[:limit - 1] + ".") for i, l in enumerate(lines)]
        text = "\n".join(lines)
        limit -= 2
    return text[:MAX_SCREEN]


def CON(ctx, *lines):
    flash, ctx.flash = ctx.flash, ""
    return "CON " + _fit(([flash] if flash else []) + list(lines))


def END(ctx, *lines):
    return "END " + _fit(list(lines))


def short(text, n=22):
    text = (text or "").strip()
    return text if len(text) <= n else text[:n]


def M(n):
    return S.fmt_ar(n)


def st(ctx, status):
    return ctx.L(status.replace("_", " "))


def back(ctx, exit_too=False):
    return ["97. " + ctx.L("Back")] + (["99. " + ctx.L("Exit")] if exit_too else [])


# ── generic steps ───────────────────────────────────────────────────────────
def _pages(render, count):
    """Greedy pages: as many numbered items per screen as fit in 182 characters."""
    if len(render(range(count), False)) <= MAX_SCREEN:
        return [list(range(count))]
    pages, cur = [], []
    for i in range(count):
        if cur and len(render(cur + [i], True)) > MAX_SCREEN:
            pages.append(cur)
            cur = [i]
        else:
            cur.append(i)
    return pages + [cur] if cur else pages or [[]]


def menu(ctx, title, labels, back_line=True, root=False):
    """Numbered menu. Everything on one screen when it fits (no paging); '98. More' only when it must."""
    def render(idxs, more, spaced=True):
        # spacing is shown when it fits; paging is measured without it
        gap = [BLANK] if spaced else []
        lines = [title] + gap + [f"{i + 1}. {labels[i]}" for i in idxs] + gap
        if more:
            lines.append("98. " + ctx.L("More"))
        if root:
            lines.append("99. " + ctx.L("Exit"))
        elif back_line:
            lines += back(ctx)
        return "\n".join(l for l in lines if l)

    pages, page = _pages(lambda idxs, more: render(
        idxs, more, False), len(labels)), 0
    while True:
        idxs, more = pages[page], page + 1 < len(pages)
        ctx.secret = False
        tok = yield CON(ctx, *render(idxs, more).split("\n"))
        if tok == "98" and more:
            page += 1
            continue
        if tok.isdigit() and 1 <= int(tok) <= len(labels) and (int(tok) - 1) in idxs:
            return int(tok) - 1
        ctx.rejected, ctx.flash = True, ctx.L("Invalid choice")


def entry(ctx, prompt, validate, secret=False):
    while True:
        ctx.secret = secret
        tok = yield CON(ctx, prompt, BLANK, *back(ctx, True))
        ok, val = validate(tok)
        if ok:
            ctx.secret = False
            return val
        ctx.rejected, ctx.flash = True, ctx.L(val)


def confirm(ctx, *summary, yes="Confirm", no="Cancel"):
    lines = list(summary)
    while True:
        tok = yield CON(ctx, *lines, BLANK, "1. " + ctx.L(yes), "2. " + ctx.L(no))
        if tok in ("1", "2"):
            return tok == "1"
        ctx.rejected, ctx.flash = True, ctx.L("Invalid choice")


def info(ctx, *lines):
    """A read-only screen that waits for 97/00/99; any other key just repeats it."""
    while True:
        yield CON(ctx, *lines, BLANK, *back(ctx))
        ctx.rejected, ctx.flash = True, ctx.L("Invalid choice")


def v_pin(tok):
    return (True, tok) if PIN_RE.match(tok) else (False, "PIN must be 4 digits")


def v_recovery(tok):
    return (True, tok) if S.RECOVERY_RE.match(tok) else (False, "Recovery code must be 6 digits")


def v_text(min_len=2, max_len=40):
    def f(tok):
        t = S.clean_text(tok, max_len)
        return (True, t) if len(t) >= min_len and "*" not in tok else (False, "Text too short")
    return f


def v_int(lo, hi):
    def f(tok):
        return (True, int(tok)) if tok.isdigit() and lo <= int(tok) <= hi else (False, "Number out of range")
    return f


def v_flags(tok):
    """'0' for none, or the numbers of every need that applies, typed together (13 = aged 60+ and a child under 5)."""
    tok = tok.strip()
    if tok == "0":
        return True, ""
    if tok.isdigit() and len(set(tok)) == len(tok) and all("1" <= ch <= str(len(S.VULN)) for ch in tok):
        return True, S.clean_flags([S.VULN[int(ch) - 1][0] for ch in tok])
    return False, "Invalid choice"


def household_needs(ctx):
    """The household questions, in the order of the web form: needs, distance to water, usual water point."""
    while True:
        tok = yield CON(ctx, ctx.L("Who lives with you? Type all that apply, e.g. 13"),
                        *[f"{i}. {ctx.L(label)}" for i, (_, _, label) in enumerate(S.VULN, 1)], "0. " + ctx.L("None of these"), *back(ctx))
        ok, flags = v_flags(tok)
        if ok:
            break
        ctx.rejected, ctx.flash = True, ctx.L("Invalid choice")
    d = yield from menu(ctx, ctx.L("How far is your water point?"), [ctx.L(label) for _, label in S.DISTANCE])
    # read at every session, so a newly added water point appears at once
    srcs = S.operational_sources()
    j = yield from menu(ctx, ctx.L("Which water point do you use most?"), [short(x.name, 22) for x in srcs] + [ctx.L("Other"), ctx.L("Not sure")])
    if j == len(srcs):
        name = yield from entry(ctx, ctx.L("Name of the water point"), v_text(2, 60))
        return flags, S.DISTANCE[d][0], S.OTHER_SOURCE + name
    return flags, S.DISTANCE[d][0], (srcs[j].id if j < len(srcs) else None)


def v_phone(tok):
    p = S.norm_phone(tok)
    return (True, p) if p else (False, "Invalid phone number")


def _err(ctx, e):
    msgs = {"insufficient_funds": "Not enough ance. Need {need}, you have {balance}.", "slot_full": "That slot just filled. Try another.",
            "slot_blocked": "Slot blocked by maintenance.", "one_per_day": "You already have a booking that day.",
            "source_unavailable": "Water point not available.", "date_range": "Choose a day in the next 7 days.",
            "slot_unavailable": "Slot not available.", "litres_invalid": "Litres not allowed here.", "not_cancellable": "This booking cannot be cancelled.",
            "too_late": "Too late: the slot has started.", "amount_range": "Amount must be {lo} to {hi}.", "provider_invalid": "Provider not available.",
            "not_pending": "Already decided.", "bad_window": "Invalid time window.", "not_approved": "Only approved bookings."}
    db.session.rollback()
    params = {k: (M(v) if k in ("need", "balance") else v)
              for k, v in e.params.items() if k != "alts"}
    return END(ctx, ctx.L(msgs.get(e.code, "Something went wrong."), **params))


# ── session: language, registration, PIN ────────────────────────────────────
def _enroll_locked(phone):
    now = time.time()
    with _ENROLL_LOCK:
        for k in [k for k, (_, t0) in _ENROLL_FAILS.items() if now - t0 > 900]:
            _ENROLL_FAILS.pop(k, None)
        return _ENROLL_FAILS.get(phone, (0, 0))[0] >= 3 or _ENROLL_FAILS.get("*", (0, 0))[0] >= 30


def _enroll_fail(phone):
    now = time.time()
    with _ENROLL_LOCK:
        for k in (phone, "*"):
            n, t0 = _ENROLL_FAILS.get(k, (0, now))
            _ENROLL_FAILS[k] = (n + 1, t0)


def session_flow(ctx):
    """Welcome and language, then straight to the menu. Menus, balances and water points need no PIN: require_pin()
    asks for it only right before an action that moves money, changes a booking or changes the account."""
    ctx.secret = False
    while True:
        tok = yield CON(ctx, "Tongasoa eto amin'ny CWAS/Welcome to CWAS/Bienvenue sur CWAS", BLANK,
                        "Safidio ny fiteny/Choose language/Choisissez votre langue:", BLANK, "1. Malagasy", "2. English", "3. Francais",
                        BLANK, "99. Exit")
        if tok in LANG_CODES:
            ctx.lang = LANG_CODES[tok]
            break
        ctx.rejected, ctx.flash = True, "Invalid choice"
    user = ctx.user
    if user is None:
        return (yield from register_flow(ctx))
    if not user.is_active_flag:
        return END(ctx, ctx.L("This account is not active yet. Ask the administrator to activate it."))
    if user.role == "member" and not user.household:
        return END(ctx, ctx.L("Something went wrong."))
    if not user.pin_hash:
        return (yield from pin_setup_flow(ctx, user))
    ctx.home_len = ctx.consumed
    if user.role == "member":
        return (yield from member_main(ctx, user))
    return (yield from staff_main(ctx, user))


def _confirm_pin(ctx, pin):
    while True:
        again = yield from entry(ctx, ctx.L("Confirm PIN"), v_pin, secret=True)
        if again == pin:
            return again
        ctx.rejected, ctx.flash = True, ctx.L("PINs do not match")


def _new_recovery(ctx):
    """A 6-digit recovery code, typed twice. It resets a forgotten PIN without calling anyone."""
    code = yield from entry(ctx, ctx.L("Create a 6-digit recovery code") + "\n" + ctx.L("It resets a forgotten PIN. Keep it private."),
                            v_recovery, secret=True)
    while True:
        again = yield from entry(ctx, ctx.L("Confirm recovery code"), v_recovery, secret=True)
        if again == code:
            return code
        ctx.rejected, ctx.flash = True, ctx.L("Codes do not match")


def pin_setup_flow(ctx, user):
    """An account opened on the web without a PIN chooses one, and a recovery code, the first time it dials in.
    The session ends right after saving, so a later hop never replays the write."""
    pin = yield from entry(ctx, ctx.L("Create a 4-digit PIN"), v_pin, secret=True)
    yield from _confirm_pin(ctx, pin)
    code = yield from _new_recovery(ctx)
    user.pin_hash, user.pin_failed, user.pin_locked_until = generate_password_hash(
        pin), 0, None
    user.recovery_hash = generate_password_hash(code)
    S.audit("user.pin_set", "user", user.id,
            "ussd", actor=user, channel="ussd")
    S.event(user, "Your PIN was set. If this was not you, contact your coordinator.",
            "system", sms=True)
    db.session.commit()
    return END(ctx, ctx.L("PIN saved."), ctx.L("Dial {dial} again to continue.", dial=DIAL))


def require_pin(ctx, user, forgot=True):
    """The PIN, asked right before a crucial action and only once per session. Returns None to go ahead, or the END
    screen to show. With forgot=True, 0 opens PIN recovery; the action then runs with the new PIN in the same hop, so
    the reset is never replayed. Callers that still need input after the PIN pass forgot=False."""
    if ctx.pin_ok:
        return None
    if user.pin_locked_until and user.pin_locked_until > utcnow():
        return END(ctx, ctx.L("Too many wrong PINs. Try again in 15 minutes."))
    while True:
        ctx.secret = True
        tok = yield CON(ctx, ctx.L("Enter your 4-digit PIN to confirm"), BLANK,
                        *(["0. " + ctx.L("Forgot PIN")] if forgot else []), *back(ctx, True))
        ctx.secret = False
        if forgot and tok == "0":
            return (yield from forgot_pin_flow(ctx, user, resume=True))
        if not PIN_RE.match(tok):
            ctx.rejected, ctx.flash = True, ctx.L("PIN must be 4 digits")
            continue
        if check_password_hash(user.pin_hash, tok):
            user.pin_failed, ctx.pin_ok = 0, True
            return None
        return _pin_failed(ctx, user, "PIN incorrect.")


def _pin_failed(ctx, user, message):
    """A wrong PIN or recovery code ends the session, so a repeated hop never counts it twice. Three in a row lock
    the PIN for 15 minutes."""
    user.pin_failed = (user.pin_failed or 0) + 1
    if user.pin_failed >= 3:
        user.pin_locked_until, user.pin_failed = utcnow() + timedelta(minutes=15), 0
        S.audit("user.pin_lock", "user", user.id,
                "3 wrong PINs or recovery codes", actor=user, channel="ussd")
        db.session.commit()
        return END(ctx, ctx.L(message), ctx.L("Too many wrong PINs. Try again in 15 minutes."))
    db.session.commit()
    return END(ctx, ctx.L(message), ctx.L("Attempts left: {n}", n=3 - user.pin_failed))


def forgot_pin_flow(ctx, user, resume=False):
    """Forgot PIN. The recovery code sets a new PIN at once. Without it, the person asks a coordinator, who checks
    their details by phone and resets the PIN. SMS does not initiate account changes."""
    if user.pin_locked_until and user.pin_locked_until > utcnow():
        return END(ctx, ctx.L("Too many wrong PINs. Try again in 15 minutes."))
    i = yield from menu(ctx, ctx.L("Forgot PIN"), [ctx.L("I have my recovery code"), ctx.L("I do not have it")])
    if i == 1 or not user.recovery_hash:
        return (yield from pin_help_flow(ctx, user, missing=(i == 0)))
    code = yield from entry(ctx, ctx.L("Enter your 6-digit recovery code"), v_recovery, secret=True)
    if not check_password_hash(user.recovery_hash, code):
        return _pin_failed(ctx, user, "Recovery code incorrect.")
    pin = yield from entry(ctx, ctx.L("Create a new 4-digit PIN"), v_pin, secret=True)
    yield from _confirm_pin(ctx, pin)
    S.set_pin(user, pin, channel="ussd")
    db.session.commit()
    ctx.pin_ok = True
    if resume:
        return None
    return END(ctx, ctx.L("PIN changed."), ctx.L("Use it next time you dial {dial}.", dial=DIAL))


def pin_help_flow(ctx, user, missing=False):
    """No recovery code: how to reach a coordinator, and a call-back request."""
    contacts = S.staff_contacts(user, 1)
    lines = [ctx.L("No recovery code is saved.")] if missing else []
    lines.append(ctx.L("Coordinator phone: {phone}.", phone=S.local_phone(contacts[0])) if contacts
                 else ctx.L("Request a coordinator call through this menu."))
    ok = yield from confirm(ctx, *lines, yes="Ask for a call", no="Exit")
    if not ok:
        return END(ctx, ctx.L("Thank you for using CWAS."))
    sent = S.request_pin_help(user, "ussd")
    db.session.commit()
    return END(ctx, ctx.L("Request sent. A coordinator will call you to check your details and reset your PIN.") if sent
               else ctx.L("A request is already open. A coordinator will call you soon."))


def register_flow(ctx):
    i = yield from menu(ctx, ctx.L("Register as:"), [ctx.L("Household member"), ctx.L("Community coordinator"), ctx.L("Help")])
    if i == 2:
        yield from info(ctx, ctx.L("Book a slot, pay by mobile money, no queue."), ctx.L("Register to start. SMS help: {sms}", sms=SHORTCODE))
        return END(ctx, ctx.L("Thank you for using CWAS."))
    role = "member"
    if i == 1:
        while True:
            if _enroll_locked(ctx.phone):
                return END(ctx, ctx.L("Too many attempts. Try again in 15 minutes."))
            code = yield from entry(ctx, ctx.L("Enter enrollment code"), lambda t: (True, t.strip()), secret=True)
            if code and code == S.get_setting("enroll_coord"):
                role = "coordinator"
                break
            _enroll_fail(ctx.phone)
            ctx.rejected, ctx.flash = True, ctx.L("Code not recognised")
    name = yield from entry(ctx, ctx.L("Enter full name"), v_text(2, 40))
    village, size, needs = "", 4, ("", None, None)
    if role == "member":
        village = yield from entry(ctx, ctx.L("Enter village / area"), v_text(2, 40))
        size = yield from entry(ctx, ctx.L("Enter household size (number)"), v_int(1, 40))
        needs = yield from household_needs(ctx)
    pin = yield from entry(ctx, ctx.L("Create 4-digit PIN"), v_pin, secret=True)
    yield from _confirm_pin(ctx, pin)
    recovery = yield from _new_recovery(ctx)
    if User.query.filter_by(phone=ctx.phone).first():
        return END(ctx, ctx.L("An account with this phone or email already exists."))
    u = User(role=role, name=name, phone=ctx.phone, language=ctx.lang, pin_hash=generate_password_hash(pin),
             recovery_hash=generate_password_hash(recovery), is_active_flag=(role != "coordinator"))
    db.session.add(u)
    db.session.flush()
    if role == "member":
        hh = Household(user_id=u.id, name=name,
                       village=village, family_size=size)
        S.set_needs(hh, *needs)
        db.session.add(hh)
    S.audit("user.register", "user", u.id,
            f"{role} via ussd", actor=u, channel="ussd")
    if role == "coordinator":
        S.event(u, "Welcome {name}! Your coordinator account is waiting for approval.",
                "system", sms=True, name=S.first_name(name))
    else:
        S.event(u, "Welcome {name}! Dial {dial} to book a slot.",
                "system", sms=True, name=S.first_name(name), dial=DIAL)
    db.session.commit()
    return END(ctx, ctx.L("Welcome {name}! Your coordinator account is waiting for approval.", name=S.first_name(name))) if role == "coordinator" else END(ctx, ctx.L("Welcome {name}!", name=S.first_name(name)), ctx.L("Dial {dial} to book a slot.", dial=DIAL))


# ── member ──────────────────────────────────────────────────────────────────
MEMBER_MENU = ["Deposit funds", "Book a slot", "My bookings", "Cancel booking", "Balance", "Notifications", "Water points", "My profile",
               "Help", "Receipts"]


def member_main(ctx, user):
    h = user.household
    flows = [deposit_flow, book_flow, bookings_flow, cancel_flow, balance_flow,
             notifications_flow, sources_flow, profile_flow, member_help, receipts_flow]
    items = list(zip(MEMBER_MENU, flows))
    while True:
        db.session.refresh(h)
        i = yield from menu(ctx, ctx.L("Hello {name}", name=S.first_name(user.name)), [ctx.L(n) for n, _ in items], root=True)
        out = yield from items[i][1](ctx, user, h)
        if out:
            return out


def deposit_flow(ctx, user, h):
    provs = [("orange", "Orange Money"), ("airtel", "Airtel Money")] + \
        ([("cash", "Cash / agent")] if S.get_setting("cash_enabled") == "1" else [])
    i = yield from menu(ctx, ctx.L("Deposit funds"), [ctx.L(l) for _, l in provs])
    provider, label = provs[i]
    lo, hi = S.get_int("min_deposit"), S.get_int("max_deposit")
    presets = [1000, 2000, 5000, 10000, 20000, 50000]
    j = yield from menu(ctx, ctx.L("Choose amount"), [M(a) for a in presets] + [ctx.L("Other amount")])
    if j == len(presets):
        amount = yield from entry(ctx, ctx.L("Enter amount in MGA") + "\n" + ctx.L("Min {min}", min=lo), v_int(lo, hi))
    else:
        amount = presets[j]
    ok = yield from confirm(ctx, ctx.L("Deposit {amount}", amount=M(amount)), ctx.L("via {provider}", provider=ctx.L(label)))
    if not ok:
        return END(ctx, ctx.L("Cancelled."))
    stop = yield from require_pin(ctx, user)
    if stop:
        return stop
    try:
        txn = S.deposit(h, amount, provider, user, "ussd")
        db.session.commit()
    except S.ServiceError as e:
        return _err(ctx, e)
    if txn.status == "posted":
        return END(ctx, ctx.L("Deposit recorded"), ctx.L(label), "+" + M(amount), f"Ref: {txn.reference}", ctx.L("Your balance is {balance}", balance=M(h.balance)))
    return END(ctx, ctx.L("Deposit pending"), ctx.L(label), M(amount), f"Ref: {txn.reference}", ctx.L("Waiting for confirmation."))


def _day_label(ctx, d, i):
    return ctx.L("Today") if i == 0 else ctx.L("Tomorrow") if i == 1 else d.strftime("%m-%d")


def book_flow(ctx, user, h):
    srcs = S.operational_sources()
    if not srcs:
        return END(ctx, ctx.L("No water point is open now."))
    i = yield from menu(ctx, ctx.L("Select water point:"), [short(s.name, 22) for s in srcs])
    src = srcs[i]
    days = S.booking_days()
    j = yield from menu(ctx, ctx.L("Select day:"), [_day_label(ctx, d, n) for n, d in enumerate(days)])
    day = days[j]
    slots = S.slot_list(src, day, only_open=True)
    if not slots:
        return END(ctx, ctx.L("No free slot that day."))
    k = yield from menu(ctx, ctx.L("Select slot:"), [f"{s['label']} ({s['free']})" for s in slots])
    slot = slots[k]
    opts = S.litre_options(src)
    quotes = [S.price_quote(h, src, l)[0] for l in opts]
    q = yield from menu(ctx, ctx.L("Quantity:"), [f"{l} L ({M(p)})" for l, p in zip(opts, quotes)])
    litres, amount = opts[q], quotes[q]
    db.session.refresh(h)
    if h.balance < amount:
        return END(ctx, ctx.L("Not enough balance. Need {need}, you have {balance}.", need=M(amount), balance=M(h.balance)), ctx.L("Deposit funds first: dial {dial}.", dial=DIAL))
    ok = yield from confirm(ctx, ctx.L("Confirm booking"), short(src.name, 20), f"{day:%Y-%m-%d} {slot['label']}", f"{litres} L - {M(amount)}",
                            ctx.L("Wallet: {balance}", balance=M(h.balance)), yes="Pay from wallet")
    if not ok:
        return END(ctx, ctx.L("Cancelled."))
    stop = yield from require_pin(ctx, user)
    if stop:
        return stop
    try:
        b = S.create_booking(
            h, src.id, day, slot["start_min"], litres, "ussd", user)
        db.session.commit()
    except S.ServiceError as e:
        return _err(ctx, e)
    return END(ctx, ctx.L("Booked!"), f"Ref: {b.ref}", short(src.name, 18), f"{day:%Y-%m-%d} {slot['label']}", f"{litres} L - {M(amount)}",
               ctx.L("Status: {status}", status=ctx.L("pending approval") if b.status == "pending" else st(ctx, b.status)))


def _my_bookings(h, limit=6, only_open=False):
    q = Booking.query.filter_by(household_id=h.id)
    if only_open:
        q = q.filter(Booking.status.in_(("pending", "approved")),
                     Booking.date >= S.today_local())
    return q.order_by(Booking.date.desc(), Booking.start_min.desc()).limit(limit).all()


def bookings_flow(ctx, user, h):
    rows = _my_bookings(h)
    if not rows:
        yield from info(ctx, ctx.L("You have no bookings yet."))
        return None
    i = yield from menu(ctx, ctx.L("Your bookings:"), [f"{b.ref} {st(ctx, b.status)} {b.date:%m-%d}" for b in rows])
    b = rows[i]
    yield from info(ctx, b.ref, short(b.source.name, 24), f"{b.date:%Y-%m-%d} {S.fmt_min(b.start_min)}-{S.fmt_min(b.end_min)}",
                    f"{b.litres} L - {M(b.amount)}", ctx.L("Status: {status}",
                                                           status=st(ctx, b.status)),
                    short(b.decision_note, 40) if b.status == "denied" and b.decision_note else "")
    return None


def cancel_flow(ctx, user, h):
    rows = _my_bookings(h, 6, only_open=True)
    if not rows:
        return END(ctx, ctx.L("No booking can be cancelled."))
    i = yield from menu(ctx, ctx.L("Cancel which?"), [f"{b.ref} {b.date:%m-%d} {S.fmt_min(b.start_min)}" for b in rows])
    b = rows[i]
    ok = yield from confirm(ctx, ctx.L("Cancel {ref}?", ref=b.ref), yes="Confirm", no="Keep")
    if not ok:
        return END(ctx, ctx.L("Kept."))
    stop = yield from require_pin(ctx, user)
    if stop:
        return stop
    try:
        S.cancel_booking(b, user, "ussd")
        db.session.commit()
    except S.ServiceError as e:
        return _err(ctx, e)
    db.session.refresh(h)
    return END(ctx, ctx.L("Cancelled {ref}. Eligible payment was refunded once.", ref=b.ref), ctx.L("Your balance is {balance}", balance=M(h.balance)))


def balance_flow(ctx, user, h):
    yield from info(ctx, ctx.L("Your balance is {balance}", balance=M(h.balance)))
    return None


_TITLES = [("Booked ", "Booking created"), ("Booking {ref} is approved", "Booking approved"), ("Booking {ref} was cancelled", "Booking cancelled"),
           ("Booking {ref} was not approved", "Booking denied"), ("Booking {ref} expired",
                                                                  "Booking expired"), ("Booking {ref} was marked", "Not collected"),
           ("Deposit {ref} of", "Deposit pending"), ("Deposit ", "Deposit recorded"), (
               "Your deposit {ref} of {amount} was not", "Deposit rejected"),
           ("Your deposit", "Deposit confirmed"), ("Water collected",
                                                   "Water collected"), ("Maintenance", "Maintenance notice"), ("Welcome", "Welcome"),
           ("Your PIN was changed", "PIN changed"), ("Your PIN was reset",
                                                     "PIN reset"), ("Your PIN", "PIN set"),
           ("Your recovery code", "Recovery code changed"), ("Your profile",
                                                             "Profile updated"), ("You asked for help", "PIN help"),
           ("PIN help", "PIN help"), ("Language set", "Language"), ("Your priority", "Priority updated")]


def _title(ctx, n):
    if n.body and not n.key:
        return ctx.L("Announcement")
    for prefix, title in _TITLES:
        if n.key.startswith(prefix):
            return ctx.L(title)
    return short(S.render_notification(n, ctx.lang), 22)


def notifications_flow(ctx, user, h):
    rows = Notification.query.filter_by(user_id=user.id).order_by(
        Notification.id.desc()).limit(6).all()
    if not rows:
        yield from info(ctx, ctx.L("No notifications."))
        return None
    i = yield from menu(ctx, ctx.L("Notifications:"), [("* " if not n.is_read else "") + _title(ctx, n) for n in rows])
    n = rows[i]
    n.is_read = True
    db.session.commit()
    yield from info(ctx, S.render_notification(n, ctx.lang)[:140])
    return None


_STATE = {"operational": "OPER", "maintenance": "MAIN", "closed": "CLOS"}


def sources_flow(ctx, user, h=None):
    srcs = WaterSource.query.order_by(WaterSource.name).all()
    if not srcs:
        yield from info(ctx, ctx.L("No water points."))
        return None
    i = yield from menu(ctx, ctx.L("Water points:"), [f"{short(s.name, 18)} [{_STATE.get(s.status, '?')}]" for s in srcs])
    s = srcs[i]
    yield from info(ctx, short(s.name, 24), ctx.L("{name} is open {a} to {b}.", name=_STATE.get(s.status, "?"), a=S.fmt_min(s.open_min), b=S.fmt_min(s.close_min)),
                    f"{M(s.tariff_per_100l)} / 100 L", f"{s.slot_minutes} min, {s.slot_capacity}/slot")
    return None


def language_flow(ctx, user, h=None):
    i = yield from menu(ctx, ctx.L("Language:"), LANG_NAMES)
    code = LANG_CODES[str(i + 1)]
    user.language = code
    S.audit("user.language", "user", user.id, code, actor=user, channel="ussd")
    # in the app only: not worth an SMS
    S.notify(user, "Language set to {code}.", "system", code=code.upper())
    db.session.commit()
    return END(ctx, tt("Language set to {code}.", code, code=code.upper()))


def member_help(ctx, user, h):
    contacts = S.staff_contacts(user, 1)
    yield from info(ctx, ctx.L("00 main menu, 97 back, 99 exit."), ctx.L("SMS {sms}: HELP for commands.", sms=SHORTCODE),
                    ctx.L("Coordinator: {phone}", phone=S.local_phone(contacts[0])) if contacts else "")
    return None


def receipts_flow(ctx, user, h):
    rows = WalletTxn.query.filter_by(household_id=h.id, status="posted").filter(
        WalletTxn.kind != "adjustment").order_by(WalletTxn.id.desc()).limit(5).all()
    if not rows:
        yield from info(ctx, ctx.L("No receipts yet."))
        return None
    i = yield from menu(ctx, ctx.L("Receipts"), [f"{r.reference} {'+' if r.amount > 0 else ''}{r.amount:,}" for r in rows])
    r = rows[i]
    ok = yield from confirm(ctx, r.reference, f"{ctx.L(r.kind.replace('_', ' '))} {'+' if r.amount > 0 else ''}{M(r.amount)}",
                            (r.created_at + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M"), ctx.L(
                                "Your balance is {balance}", balance=M(r.balance_after or 0)),
                            yes="Send to my SMS", no="Back")
    if ok:
        S.send_sms(user.phone, ctx.L("Receipt {ref}: {kind} {amount}, balance {balance}.", ref=r.reference, kind=ctx.L(r.kind.replace("_", " ")),
                                     amount=M(r.amount), balance=M(r.balance_after or 0)), user)
        db.session.commit()
        return END(ctx, ctx.L("Receipt sent by SMS."))
    return None


# ── my profile: read and change the account; the PIN guards every change ─────
def profile_flow(ctx, user, h=None):
    """My profile: what the web profile offers, on any phone. Members can also delete their account here."""
    member = user.role == "member"
    items = [("View profile", view_profile), ("Change name", change_name)]
    if member:
        items += [("Village / area", change_village),
                  ("Household size", change_size)]
    items += [("Language", language_flow), ("Change PIN", change_pin),
              ("Recovery code", change_recovery), ("Forgot PIN", profile_forgot)]
    if member:
        items.append(("Delete account", delete_flow))
    i = yield from menu(ctx, ctx.L("My profile"), [ctx.L(n) for n, _ in items])
    return (yield from items[i][1](ctx, user, h))


def view_profile(ctx, user, h=None):
    lines = [short(user.name, 40), S.local_phone(user.phone)]
    if h:
        lines += [f"{short(h.village, 20)}, " + ctx.L("{n} people", n=h.family_size),
                  ctx.L("Priority: {level}", level=ctx.L(h.priority_level))]
    else:
        lines.append(ctx.L(user.role))
    lines += [ctx.L("Language: {code}", code=user.language.upper()),
              ctx.L("Recovery code: {state}", state=ctx.L("saved") if user.recovery_hash else ctx.L("not set"))]
    yield from info(ctx, *lines)
    return None


def _save_profile(ctx, user, shown, apply):
    """Shared end of a profile change: confirm, PIN, save, audit and an in-app note. Ends the session."""
    ok = yield from confirm(ctx, ctx.L("Save this change?"), shown, yes="Save", no="Cancel")
    if not ok:
        return END(ctx, ctx.L("Cancelled."))
    stop = yield from require_pin(ctx, user)
    if stop:
        return stop
    apply()
    S.audit("user.profile", "user", user.id,
            "ussd", actor=user, channel="ussd")
    S.notify(user, "Your profile was updated.", "system")
    db.session.commit()
    return END(ctx, ctx.L("Saved."), shown)


def change_name(ctx, user, h=None):
    name = yield from entry(ctx, ctx.L("Enter full name"), v_text(2, 40))

    def apply():
        user.name = name
        if h:
            h.name = name
    return (yield from _save_profile(ctx, user, name, apply))


def change_village(ctx, user, h):
    village = yield from entry(ctx, ctx.L("Enter village / area"), v_text(2, 40))
    return (yield from _save_profile(ctx, user, village, lambda: setattr(h, "village", village)))


def change_size(ctx, user, h):
    size = yield from entry(ctx, ctx.L("Enter household size (number)"), v_int(1, 40))
    return (yield from _save_profile(ctx, user, ctx.L("{n} people", n=size), lambda: setattr(h, "family_size", size)))


def change_pin(ctx, user, h=None):
    # the current PIN first, so a borrowed phone cannot change it
    stop = yield from require_pin(ctx, user, forgot=False)
    if stop:
        return stop
    pin = yield from entry(ctx, ctx.L("Create a new 4-digit PIN"), v_pin, secret=True)
    yield from _confirm_pin(ctx, pin)
    S.set_pin(user, pin, channel="ussd")
    db.session.commit()
    return END(ctx, ctx.L("PIN changed."))


def change_recovery(ctx, user, h=None):
    stop = yield from require_pin(ctx, user, forgot=False)
    if stop:
        return stop
    code = yield from _new_recovery(ctx)
    S.set_recovery(user, code, channel="ussd")
    db.session.commit()
    return END(ctx, ctx.L("Recovery code saved. Keep it private."))


def profile_forgot(ctx, user, h=None):
    return (yield from forgot_pin_flow(ctx, user))


def delete_flow(ctx, user, h):
    """Delete my account: the same erasure as the web. Open bookings are cancelled and refunded, personal data is
    removed, and any balance is recorded as a cash refund the coordinator owes."""
    bal = h.balance if h else 0
    ok = yield from confirm(ctx, ctx.L("Delete your account?"), ctx.L("Your personal data is removed."),
                            ctx.L("Your coordinator refunds {amount} in cash.", amount=M(
                                bal)) if bal > 0 else "",
                            yes="Delete my account", no="Keep")
    if not ok:
        return END(ctx, ctx.L("Kept."))
    stop = yield from require_pin(ctx, user)
    if stop:
        return stop
    # sent before the number is erased
    S.send_sms(user.phone, ctx.L(
        "Your CWAS account and personal data were deleted."), user)
    current_app.extensions["cwas.erase_account"](user, "ussd")
    db.session.commit()
    ctx.erased = True
    return END(ctx, ctx.L("Your account and personal data were deleted."))


# ── coordinator / admin ─────────────────────────────────────────────────────
def staff_main(ctx, user):
    items = [("Pending queue", queue_flow), ("Approve", approve_flow), ("Deny", deny_flow), ("Mark collected", collect_flow), ("Water points", staff_sources_flow),
             ("Register household", register_household_flow), ("Operations summary",
                                                               summary_flow), ("My profile", profile_flow), ("Help", staff_help),
             ("Cash deposit", cash_flow), ("Announcement", announce_flow), ("Reset household PIN", reset_member_pin_flow)]
    while True:
        i = yield from menu(ctx, ctx.L("Hello {name}", name=S.first_name(user.name)), [ctx.L(n) for n, _ in items], root=True)
        out = yield from items[i][1](ctx, user)
        if out:
            return out


def _pending(limit=6):
    return Booking.query.filter_by(status="pending").order_by(Booking.date, Booking.start_min, Booking.priority_score.desc()).limit(limit).all()


def _pick(ctx, title, rows, empty):
    if not rows:
        yield from info(ctx, ctx.L(empty))
        return None
    i = yield from menu(ctx, title, [f"{b.ref} {short(b.household.name, 8)} {b.litres}L" for b in rows])
    return rows[i]


def _decide(ctx, user, b, approve, reason=""):
    try:
        S.decide_booking(b, approve, reason, user, "ussd")
        db.session.commit()
    except S.ServiceError as e:
        return _err(ctx, e)
    return END(ctx, ctx.L("Approved {ref}.", ref=b.ref) if approve else ctx.L("Denied {ref}. Household refunded.", ref=b.ref))


def queue_flow(ctx, user):
    b = yield from _pick(ctx, ctx.L("Pending ({n}):", n=Booking.query.filter_by(status="pending").count()), _pending(), "No pending bookings.")
    if not b:
        return None
    title = "\n".join([b.ref, f"{short(b.household.name, 16)} {b.household.village[:10]}", f"{short(b.source.name, 14)} {b.date:%m-%d} {S.fmt_min(b.start_min)}",
                       f"{b.litres} L {M(b.amount)} P{b.priority_score}"])
    i = yield from menu(ctx, title, [ctx.L("Approve"), ctx.L("Deny")])
    reason = ""
    if i == 1:
        r = yield from menu(ctx, ctx.L("Reason:"), [ctx.L(x) for x in DENY_REASONS])
        reason = DENY_REASONS[r]
    stop = yield from require_pin(ctx, user)
    return stop or _decide(ctx, user, b, i == 0, reason)


def approve_flow(ctx, user):
    b = yield from _pick(ctx, ctx.L("Approve which?"), _pending(), "No pending bookings.")
    if not b:
        return None
    ok = yield from confirm(ctx, ctx.L("Approve {ref}?", ref=b.ref), f"{short(b.household.name, 14)} {b.litres} L")
    if not ok:
        return END(ctx, ctx.L("Kept."))
    stop = yield from require_pin(ctx, user)
    return stop or _decide(ctx, user, b, True)


def deny_flow(ctx, user):
    b = yield from _pick(ctx, ctx.L("Deny which?"), _pending(), "No pending bookings.")
    if not b:
        return None
    r = yield from menu(ctx, ctx.L("Reason:"), [ctx.L(x) for x in DENY_REASONS])
    stop = yield from require_pin(ctx, user)
    return stop or _decide(ctx, user, b, False, DENY_REASONS[r])


def collect_flow(ctx, user):
    rows = Booking.query.filter_by(status="approved").filter(
        Booking.date <= S.today_local()).order_by(Booking.date, Booking.start_min).limit(6).all()
    b = yield from _pick(ctx, ctx.L("Mark which?"), rows, "Nothing to mark.")
    if not b:
        return None
    i = yield from menu(ctx, b.ref, [ctx.L("Collected"), ctx.L("no show")])
    stop = yield from require_pin(ctx, user)
    if stop:
        return stop
    try:
        (S.mark_collected if i == 0 else S.mark_no_show)(b, user, "ussd")
        db.session.commit()
    except S.ServiceError as e:
        return _err(ctx, e)
    return END(ctx, ctx.L("Saved."))


def staff_sources_flow(ctx, user):
    srcs = WaterSource.query.order_by(WaterSource.name).all()
    i = yield from menu(ctx, ctx.L("Water points:"), [f"{short(s.name, 18)} [{_STATE.get(s.status, '?')}]" for s in srcs])
    s = srcs[i]
    j = yield from menu(ctx, short(s.name, 22), [ctx.L("Set operational"), ctx.L("Set closed"), ctx.L("Block 2 h"), ctx.L("Block 6 h")])
    stop = yield from require_pin(ctx, user)
    if stop:
        return stop
    if j in (0, 1):
        s.status = "operational" if j == 0 else "closed"
        S.audit("source.status", "source", s.id,
                s.status, actor=user, channel="ussd")
        db.session.commit()
        return END(ctx, ctx.L("{name} is now {status}.", name=short(s.name, 18), status=ctx.L(s.status)))
    start = S.now_local()
    try:
        _, n = S.schedule_maintenance(
            s, start, start + timedelta(hours=2 if j == 2 else 6), "Blocked by USSD", user, "ussd")
        db.session.commit()
    except S.ServiceError as e:
        return _err(ctx, e)
    return END(ctx, ctx.L("Maintenance set. {n} booking(s) moved and refunded.", n=n))


def register_household_flow(ctx, user):
    name = yield from entry(ctx, ctx.L("Household name"), v_text(2, 40))
    while True:
        phone = yield from entry(ctx, ctx.L("Household phone number"), v_phone)
        if not User.query.filter_by(phone=phone).first():
            break
        ctx.rejected, ctx.flash = True, ctx.L(
            "An account with this phone or email already exists.")
    village = yield from entry(ctx, ctx.L("Enter village / area"), v_text(2, 40))
    size = yield from entry(ctx, ctx.L("Enter household size (number)"), v_int(1, 40))
    flags, dist, home = yield from household_needs(ctx)
    li = yield from menu(ctx, ctx.L("Language:"), LANG_NAMES)
    pin = yield from entry(ctx, ctx.L("Create 4-digit PIN"), v_pin, secret=True)
    ok = yield from confirm(ctx, short(name, 20), phone, f"{short(village, 14)}, {size}", LANG_NAMES[li])
    if not ok:
        return END(ctx, ctx.L("Cancelled."))
    stop = yield from require_pin(ctx, user)
    if stop:
        return stop
    u = User(role="member", name=name, phone=phone, language=LANG_CODES[str(
        li + 1)], pin_hash=generate_password_hash(pin))
    db.session.add(u)
    db.session.flush()
    hh = Household(user_id=u.id, name=name, village=village,
                   family_size=size, priority_level=S.reported_level(flags))
    # the coordinator is with the household, so the level counts as checked
    S.set_needs(hh, flags, dist, home)
    hh.needs_review = False
    db.session.add(hh)
    S.audit("user.register", "user", u.id,
            "household registered by staff via ussd", actor=user, channel="ussd")
    S.event(u, "Welcome {name}! Dial {dial} to book a slot.",
            "system", sms=True, name=S.first_name(name), dial=DIAL)
    db.session.commit()
    return END(ctx, ctx.L("Registered {name}.", name=short(name, 18)), phone)


def reset_member_pin_flow(ctx, user):
    """A household forgot its PIN and has no recovery code: check the caller's details, then send a temporary PIN
    to the household's own phone. The coordinator never sees it."""
    while True:
        phone = yield from entry(ctx, ctx.L("Household phone number"), v_phone)
        m = User.query.filter_by(
            phone=phone, role="member", is_active_flag=True).first()
        if m and m.household:
            break
        ctx.rejected, ctx.flash = True, ctx.L("No member with that number.")
    h = m.household
    ok = yield from confirm(ctx, ctx.L("Check with the caller:"), short(m.name, 30), f"{short(h.village, 18)}, " + ctx.L("{n} people", n=h.family_size),
                            yes="Details match, reset PIN", no="Cancel")
    if not ok:
        return END(ctx, ctx.L("Cancelled."))
    stop = yield from require_pin(ctx, user)
    if stop:
        return stop
    S.reset_pin_by_staff(m, user, "ussd")
    db.session.commit()
    return END(ctx, ctx.L("PIN reset for {name}.", name=S.first_name(m.name)), ctx.L("A temporary PIN was sent to their phone by SMS."))


def summary_flow(ctx, user):
    t = S.today_local()
    today = Booking.query.filter_by(date=t).all()

    def c(s):
        return sum(1 for b in today if b.status == s)
    yield from info(ctx, ctx.L("Today {d}", d=t.strftime("%m-%d")), ctx.L("Pending {a}, approved {b}", a=Booking.query.filter_by(status="pending").count(), b=c("approved")),
                    ctx.L("Collected {a}, litres {b}", a=c("collected"), b=sum(
                        b.litres for b in today if b.status in ("approved", "collected"))),
                    ctx.L("Deposits waiting {n}, households {h}", n=WalletTxn.query.filter_by(kind="deposit", status="pending").count(), h=Household.query.count()))
    return None


def staff_help(ctx, user):
    yield from info(ctx, ctx.L("SMS {sms}: PENDING and SOURCES only for inquiries.", sms=SHORTCODE), ctx.L("Use PIN-protected USSD for approvals and registration."))
    return None


def cash_flow(ctx, user):
    while True:
        phone = yield from entry(ctx, ctx.L("Household phone number"), v_phone)
        m = User.query.filter_by(
            phone=phone, role="member", is_active_flag=True).first()
        if m and m.household:
            break
        ctx.rejected, ctx.flash = True, ctx.L("No member with that number.")
    lo = S.get_int("min_deposit")
    amount = yield from entry(ctx, ctx.L("Cash received in MGA") + "\n" + ctx.L("Min {min}", min=lo), v_int(lo, S.get_int("max_deposit")))
    ok = yield from confirm(ctx, ctx.L("Add {amount} cash to {name}?", amount=M(amount), name=short(m.name, 14)))
    if not ok:
        return END(ctx, ctx.L("Cancelled."))
    stop = yield from require_pin(ctx, user)
    if stop:
        return stop
    try:
        txn = S.deposit(m.household, amount, "cash", user, "staff")
        db.session.commit()
    except S.ServiceError as e:
        return _err(ctx, e)
    return END(ctx, ctx.L("Deposit recorded"), "+" + M(amount), f"Ref: {txn.reference}", ctx.L("Your balance is {balance}", balance=M(m.household.balance)))


def announce_flow(ctx, user):
    msg = yield from entry(ctx, ctx.L("Type the announcement (max 100)"), v_text(3, 100))
    ok = yield from confirm(ctx, ctx.L("Send to all households?"), short(msg, 60))
    if not ok:
        return END(ctx, ctx.L("Cancelled."))
    stop = yield from require_pin(ctx, user)
    if stop:
        return stop
    n = broadcast(msg, user, "ussd")
    db.session.commit()
    return END(ctx, ctx.L("Sent to {n} households.", n=n))


def broadcast(message, actor, channel="web"):
    members = User.query.filter_by(role="member", is_active_flag=True).all()
    phones = []
    for m in members:
        S.notify(m, "", "announcement", body=message)
        if m.phone:
            phones.append(m.phone)
    S.send_sms_bulk(phones, message[:160])
    n = len(members)
    S.audit("announcement.send", "announcement", "",
            f"{n} recipients: {message[:80]}", actor=actor, channel=channel)
    return n


# ── runner ──────────────────────────────────────────────────────────────────
def _start(ctx, factory):
    ctx.consumed, ctx.rejected, ctx.flash = 0, False, ""
    gen = factory(ctx)
    return gen, next(gen)


def _replay(ctx, factory, path):
    gen, screen = _start(ctx, factory)
    for p in path:
        ctx.rejected = False
        ctx.consumed += 1
        try:
            screen = gen.send(p)
        except StopIteration as stop:
            return None, stop.value or END(ctx, ctx.L("Done."))
    return gen, screen


def _run(ctx, factory, tokens):
    gen, screen = _start(ctx, factory)
    path = []
    for tok in tokens:
        if tok == "":
            continue
        if tok == "99":
            ctx.trail.append("99")
            return END(ctx, ctx.L("Thank you for using CWAS."))
        if tok in ("00", "97"):
            ctx.trail.append(tok)
            if tok == "97":
                if ctx.home_len and len(path) <= ctx.home_len:
                    continue  # already at the role's home menu
                if path:
                    path.pop()
            else:
                path = path[:ctx.home_len] if ctx.home_len else []
            gen, screen = _replay(ctx, factory, path)
            if gen is None:
                return screen
            continue
        ctx.trail.append("*" if ctx.secret else tok[:24])
        ctx.rejected, ctx.flash = False, ""
        ctx.consumed += 1
        try:
            screen = gen.send(tok)
        except StopIteration as stop:
            return stop.value or END(ctx, ctx.L("Done."))
        if not ctx.rejected:
            path.append(tok)
    return screen or END(ctx, ctx.L("Done."))


def handle_ussd(session_id, phone, text, channel="telco"):
    """One hop of a USSD session. Returns the CON/END body. A repeated identical hop returns the stored answer."""
    phone = S.norm_phone(phone)
    text = (text or "").strip()
    session_id = (session_id or "none")[:80]
    key = hashlib.sha256(f"{session_id}|{text}".encode()).hexdigest()[:24]
    sess = UssdSession.query.filter_by(session_id=session_id).first()
    if sess and sess.ended and sess.trail.endswith("#" + key):
        return sess.last_response
    user = User.query.filter_by(phone=phone).first() if phone else None
    ctx = Ctx(user.language if user else "mg", user, phone, channel)
    try:
        out = "END Invalid phone." if not phone else _run(
            ctx, session_flow, text.split("*") if text else [])
    except Exception:  # never leak a traceback to a handset
        db.session.rollback()
        log.exception("USSD failure")
        out = "END Service unavailable. Please retry."
    _log_session(session_id, phone, channel, ctx, out, key)
    return out


def _log_session(session_id, phone, channel, ctx, out, key):
    try:
        sess = UssdSession.query.filter_by(session_id=session_id).first()
        if not sess:
            sess = UssdSession(session_id=session_id, phone="deleted" if ctx.erased else (
                phone or "?"), channel=channel)
            db.session.add(sess)
        sess.hops = (sess.hops or 0) + 1
        sess.updated_at = utcnow()
        sess.ended = out.startswith("END")
        sess.trail = (" > ".join(ctx.trail[-12:]))[:330] + "#" + key
        sess.last_response = out[:200]
        db.session.commit()
    except Exception:
        db.session.rollback()


# ── SMS: read-only inquiries (all account and transaction changes use authenticated USSD/web) ──
def _find_booking(ref, user=None):
    b = Booking.query.filter_by(ref=ref.upper()).first()
    if b and user is not None and user.role == "member" and b.household_id != user.household.id:
        return None
    return b


def st_l(lang, status):
    return tt(status.replace("_", " "), lang)


def _sms_err(lang, e):
    class _C:
        pass
    c = _C()
    c.L = lambda s, **kw: tt(s, lang, **kw)
    return _err(c, e)[4:]


_SMS_BLOCKED = frozenset({
    "BOOK", "CANCEL", "DEPOSIT", "APPROVE", "DENY", "COLLECT",
    "REG", "REGISTER", "LANG", "PIN", "FORGOT", "RESET", "RECOVER", "LOST",
})
_SMS_REDIRECT = "SMS is for inquiries only. Dial {dial} or use the website to make account or booking changes."
_SMS_HELP_MEMBER = "BALANCE, SOURCES, BOOKINGS, BOOKING <ref>, RECEIPT <ref>, NOTICES, PROFILE. Changes: dial {dial}."
_SMS_HELP_STAFF = "PENDING, SOURCES, BOOKING <ref>, NOTICES, PROFILE. Approvals and changes: dial {dial}."


def handle_sms(phone, text):
    """Answer read-only SMS inquiries; no SMS command may change an account, booking or payment.

    Sender ID and telco webhook authentication do not authenticate individual actions. Never request or
    accept a PIN over SMS. A rejected command is redacted in the incoming SMS log.
    """
    phone = S.norm_phone(phone)
    body = S.clean_text(text, 300)
    parts = body.split(None, 1)
    cmd, rest = (parts[0].upper() if parts else "HELP"), (parts[1].strip() if len(parts) > 1 else "")
    words = set(body.upper().split())
    pin_help = "PIN" in words and words <= {"PIN", "HELP", "FORGOT", "RESET", "LOST"}
    blocked = cmd in _SMS_BLOCKED or pin_help
    db.session.add(SmsLog(direction="in", phone=phone or "?",
                          body=(f"{cmd} [blocked]" if blocked else body)[:600], status="received"))
    user = User.query.filter_by(phone=phone, is_active_flag=True).first() if phone else None
    lang = user.language if user else "en"

    def L(message, **kw):
        return tt(message, lang, **kw)

    h = user.household if user and user.role == "member" else None
    staff = bool(user and user.role in ("coordinator", "admin"))
    reply = ""
    try:
        if blocked:
            reply = L(_SMS_REDIRECT, dial=DIAL)
        elif cmd == "HELP":
            reply = L(_SMS_HELP_STAFF if staff else _SMS_HELP_MEMBER, dial=DIAL)
        elif user is None:
            reply = L("Dial {dial} to register or sign in.", dial=DIAL)
        elif cmd in ("BALANCE", "BAL", "WALLET") and h:
            reply = L("Your balance is {balance}", balance=M(h.balance))
        elif cmd == "SOURCES":
            reply = "; ".join(f"{i}. {short(source.name, 20)} [{_STATE.get(source.status, '?')}] {S.fmt_min(source.open_min)}-{S.fmt_min(source.close_min)}"
                              for i, source in enumerate(WaterSource.query.order_by(WaterSource.name).all(), 1))
        elif cmd in ("BOOKINGS", "MYBOOKINGS") and h:
            reply = " | ".join(f"{booking.ref} {st_l(lang, booking.status)} {booking.date:%m-%d} {S.fmt_min(booking.start_min)}"
                               for booking in _my_bookings(h, 3)) or L("You have no bookings yet.")
        elif cmd == "BOOKING" and rest:
            booking = _find_booking(rest.split()[0], user)
            reply = (f"{booking.ref} {short(booking.source.name, 20)} {booking.date:%m-%d} {S.fmt_min(booking.start_min)}-{S.fmt_min(booking.end_min)} {booking.litres} L {M(booking.amount)} {st_l(lang, booking.status)}"
                     if booking else L("Booking not found."))
        elif cmd == "RECEIPT" and rest:
            booking = _find_booking(rest.split()[0], user)
            txn = WalletTxn.query.filter_by(booking_id=booking.id, kind="booking_debit", status="posted").first() if booking else None
            reply = (L("Receipt {ref}: {kind} {amount}, balance {balance}.", ref=txn.reference, kind=L("booking debit"), amount=M(txn.amount), balance=M(txn.balance_after or 0))
                     if txn else L("Receipt not found."))
        elif cmd in ("NOTICES", "NOTICE"):
            rows = Notification.query.filter_by(user_id=user.id, is_read=False).order_by(
                Notification.id.desc()).limit(3).all()
            # Reading an SMS inquiry does not mark notices read or change app notification state.
            reply = " | ".join(S.render_notification(n, lang)[:90] for n in rows) or L("No notifications.")
        elif cmd == "PROFILE":
            reply = f"{user.name}, {h.village}, {h.family_size}, {L(h.priority_level)}, {lang.upper()}" if h else f"{user.name}, {L(user.role)}"
        elif cmd == "PENDING" and staff:
            reply = " | ".join(f"{booking.ref} {short(booking.household.name, 10)} {booking.litres}L" for booking in _pending(5)) or L("No pending bookings.")
        else:
            reply = L("Unknown command. Send HELP.")
        db.session.commit()
    except S.ServiceError as e:
        db.session.rollback()
        reply = _sms_err(lang, e)
    return reply[:320]


# ── homepage demo phones ────────────────────────────────────────────────────
def household_menu_screens(lang):
    """The household main menu, every page exactly as a caller sees it. /access shows these, so it cannot drift from the engine."""
    ctx = Ctx(lang=lang, phone="+261340000000")
    gen = menu(ctx, ctx.L("Hello {name}", name="Rasoa"), [
               ctx.L(n) for n in MEMBER_MENU], root=True)
    screens = [next(gen)[4:]]
    while "\n98. " in screens[-1] and len(screens) < 6:
        screens.append(gen.send("98")[4:])
    return screens


def demo_script(lang):
    """The three demo phones on the website play screens drawn by THIS engine: the same renderers, menus, wording,
    blank lines, key numbers, water points, slots and prices a caller gets. Only the person (Rasoa), her balance and the
    booking reference are examples, and nothing is written. SMS replies use the same templates as handle_sms()."""
    from types import SimpleNamespace
    ctx = Ctx(lang=lang, phone="+261340000000")
    L = ctx.L
    def first(gen): return next(gen)[4:]  # noqa: E731  the first screen of a real step, without the CON prefix
    lk = {v: k for k, v in LANG_CODES.items()}[lang]
    welcome = first(session_flow(Ctx(lang=lang)))
    main = first(menu(ctx, L("Hello {name}", name="Rasoa"), [
                 L(n) for n in MEMBER_MENU], root=True))
    balance = 12500
    bal = first(info(ctx, L("Your balance is {balance}", balance=M(balance))))
    script = {"ui": {"cancel": L("Cancel"), "send": L("Send"), "ok": L("OK")},
              "lite": [["dial", DIAL], ["call", ""], ["screen", welcome], ["key", lk], ["screen", main], ["key", str(MEMBER_MENU.index("Balance") + 1)], ["screen", bal]]}
    srcs = S.operational_sources()
    days = S.booking_days()
    src = srcs[0] if srcs else None
    slots = S.slot_list(src, days[1], only_open=True) if src and len(
        days) > 1 else []
    nova = [["dial", DIAL], ["call", ""], ["screen", welcome], ["key", lk], [
        "screen", main], ["key", str(MEMBER_MENU.index("Book a slot") + 1)]]
    if src and slots:
        home = SimpleNamespace(priority_level="standard")
        slot = slots[0]
        opts = S.litre_options(src)
        litres = 60 if 60 in opts else opts[0]
        amount = S.price_quote(home, src, litres)[0]
        ref = "CW-4F7A9C21"
        nova += [["screen", first(menu(ctx, L("Select water point:"), [short(x.name, 22) for x in srcs]))], ["key", "1"],
                 ["screen", first(menu(ctx, L("Select day:"), [_day_label(
                     ctx, d, n) for n, d in enumerate(days)]))], ["key", "2"],
                 ["screen", first(menu(ctx, L("Select slot:"), [
                                  f"{x['label']} ({x['free']})" for x in slots]))], ["key", "1"],
                 ["screen", first(menu(ctx, L("Quantity:"), [f"{l} L ({M(S.price_quote(home, src, l)[0])})" for l in opts]))], [
            "key", str(opts.index(litres) + 1)],
            ["screen", first(confirm(ctx, L("Confirm booking"), short(src.name, 20), f"{days[1]:%Y-%m-%d} {slot['label']}", f"{litres} L - {M(amount)}",
                                     L("Wallet: {balance}", balance=M(balance)), yes="Pay from wallet"))], ["key", "1"],
            ["screen", first(require_pin(ctx, SimpleNamespace(pin_locked_until=None)))], [
            "pin", "1234"],
            ["end", END(ctx, L("Booked!"), f"Ref: {ref}", short(src.name, 18), f"{days[1]:%Y-%m-%d} {slot['label']}", f"{litres} L - {M(amount)}",
                        L("Status: {status}", status=L("pending approval")))[4:]]]
        n = [x.id for x in WaterSource.query.order_by(WaterSource.name).all()].index(src.id) + 1
        script["max"] = [["type", "BALANCE"], ["in", L("Your balance is {balance}", balance=M(balance))],
                         ["type", f"BOOK {n} TOMORROW {S.fmt_min(slot['start_min'])} {litres}"],
                         ["in", L(_SMS_REDIRECT, dial=DIAL)]]
    else:
        script["max"] = [["type", "BALANCE"], [
            "in", L("Your balance is {balance}", balance=M(balance))]]
    script["nova"] = nova
    return script
