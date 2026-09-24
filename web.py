"""Web routes: public site, auth, member app, coordinator console, admin console, USSD/SMS webhooks, simulator."""
import hashlib
import hmac
import json
import os
import secrets
import smtplib
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from functools import wraps

import re
import segno
import shutil
from flask import (abort, current_app, flash, g, jsonify, make_response, redirect, render_template, request, send_file,
                   session, url_for)
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import or_, text
from werkzeug.security import check_password_hash, generate_password_hash

import services as S
import ussd as U
import uploads as UP
from legal import DOCS, UPDATED
from i18n import LANGS, tt
from models import (AuditLog, Booking, ChatMessage, ChatThread, Household, Maintenance, Notification, PasswordReset, PilotFollower, Setting,
                    SmsLog, User, UssdSession, WalletTxn, WaterSource, db, utcnow)

BACKUP_DIR = None
DUMMY_HASH = generate_password_hash("timing-equaliser")
ERR = {
    "insufficient_funds": "Not enough balance. You need {need} and have {balance}. Add money in Wallet.",
    "slot_full": "That slot just filled up. Pick another below.", "slot_blocked": "That slot is blocked by maintenance.",
    "one_per_day": "You already have an active booking on that day.", "source_unavailable": "This water point is not available.",
    "date_range": "Choose a day within the next 7 days.", "slot_unavailable": "That slot is not available.",
    "litres_invalid": "That volume is not allowed at this water point.", "not_cancellable": "This booking can no longer be cancelled.",
    "too_late": "Too late to cancel: the slot has already started.", "amount_range": "Amount must be between {lo} and {hi}.",
    "provider_invalid": "Choose a payment method.", "not_pending": "This item was already handled.", "bad_window": "The end must be after the start.",
    "not_approved": "Only approved bookings can be marked.",
}


def T(s, **kw):
    return tt(s, g.get("lang", "en"), **kw)


def say(msg, kind="ok", **kw):
    flash(T(msg, **kw), kind)


def say_error(e):
    say(ERR.get(e.code, "Something went wrong."), "err", **{k: v for k, v in e.params.items() if k != "alts"})


def roles_required(*roles):
    def deco(fn):
        @wraps(fn)
        @login_required
        def wrapper(*a, **kw):
            if current_user.role not in roles:
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


member_required = roles_required("member")
staff_required = roles_required("coordinator", "admin")
admin_required = roles_required("admin")


def safe_next(target):
    return target if target and target.startswith("/") and not target.startswith("//") and "\\" not in target else None


def home_for(user):
    return url_for({"member": "dashboard", "coordinator": "coord_home", "admin": "admin_home"}[user.role])


def parse_date(s, default):
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return default


def home_choice(f):
    """The usual water point from a form: a listed point, "other" with the name typed beside it, or Not sure (0)."""
    v = (f.get("home_source") or "").strip()
    if v == "other":
        name = S.clean_text(f.get("home_other"), 80)
        return S.OTHER_SOURCE + name if len(name) >= 2 else 0
    home = int(v) if v.isdigit() else 0
    return home if home and db.session.get(WaterSource, home) else 0


def int_arg(name, default, lo, hi, src=None):
    try:
        v = int((src or request.form).get(name, default))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def register_routes(app):
    global BACKUP_DIR
    from extensions import csrf, limiter
    from pathlib import Path
    BACKUP_DIR = Path(app.config["INSTANCE_DIR"]) / "backups"
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

