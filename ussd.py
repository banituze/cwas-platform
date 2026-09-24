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
DENY_REASONS = ["Slot unavailable", "Maintenance", "Incomplete request", "Other, coordinator will call"]
_ENROLL_FAILS = {}
_ENROLL_LOCK = threading.Lock()


class Ctx:
    def __init__(self, lang="mg", user=None, phone="", channel="telco"):
        self.lang, self.user, self.phone, self.channel = lang, user, phone, channel
        self.rejected, self.secret, self.flash, self.trail = False, False, "", []
        self.consumed, self.home_len = 0, 0
        self.pin_ok, self.erased = False, False  # PIN checked this session; account erased (log without the phone)

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
        lines = [(l if len(l) <= limit or i == 0 or _is_nav(l) else l[:limit - 1] + ".") for i, l in enumerate(lines)]
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
        gap = [BLANK] if spaced else []  # spacing is shown when it fits; paging is measured without it
        lines = [title] + gap + [f"{i + 1}. {labels[i]}" for i in idxs] + gap
        if more:
            lines.append("98. " + ctx.L("More"))
        if root:
            lines.append("99. " + ctx.L("Exit"))
        elif back_line:
            lines += back(ctx)
        return "\n".join(l for l in lines if l)

    pages, page = _pages(lambda idxs, more: render(idxs, more, False), len(labels)), 0
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
    srcs = S.operational_sources()  # read at every session, so a newly added water point appears at once
    j = yield from menu(ctx, ctx.L("Which water point do you use most?"), [short(x.name, 22) for x in srcs] + [ctx.L("Other"), ctx.L("Not sure")])
    if j == len(srcs):
        name = yield from entry(ctx, ctx.L("Name of the water point"), v_text(2, 60))
        return flags, S.DISTANCE[d][0], S.OTHER_SOURCE + name
    return flags, S.DISTANCE[d][0], (srcs[j].id if j < len(srcs) else None)


def v_phone(tok):
    p = S.norm_phone(tok)
    return (True, p) if p else (False, "Invalid phone number")


def _err(ctx, e):
    msgs = {"insufficient_funds": "Not enough balance. Need {need}, you have {balance}.", "slot_full": "That slot just filled. Try another.",
            "slot_blocked": "Slot blocked by maintenance.", "one_per_day": "You already have a booking that day.",
            "source_unavailable": "Water point not available.", "date_range": "Choose a day in the next 7 days.",
            "slot_unavailable": "Slot not available.", "litres_invalid": "Litres not allowed here.", "not_cancellable": "This booking cannot be cancelled.",
            "too_late": "Too late: the slot has started.", "amount_range": "Amount must be {lo} to {hi}.", "provider_invalid": "Provider not available.",
            "not_pending": "Already decided.", "bad_window": "Invalid time window.", "not_approved": "Only approved bookings."}
    db.session.rollback()
    params = {k: (M(v) if k in ("need", "balance") else v) for k, v in e.params.items() if k != "alts"}
    return END(ctx, ctx.L(msgs.get(e.code, "Something went wrong."), **params))


