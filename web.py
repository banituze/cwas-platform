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

    # ── public ──────────────────────────────────────────────────────────────
    @app.get("/")
    def index():
        sources = WaterSource.query.order_by(WaterSource.name).all()
        return render_template("index.html", sources=sources)

    @app.get("/platform")
    def platform():
        return render_template("platform.html")

    @app.get("/access")
    def access():
        return render_template("access.html", menu_screens=U.household_menu_screens(g.lang))

    @app.get("/water-points")
    def water_points():
        return render_template("water_points.html", sources=WaterSource.query.order_by(WaterSource.name).all())

    @app.get("/about")
    def about():
        return render_template("about.html")

    @app.get("/terms", defaults={"doc": "terms"})
    @app.get("/privacy", defaults={"doc": "privacy"})
    @app.get("/refunds", defaults={"doc": "refunds"})
    def legal(doc):
        return render_template("legal.html", d=DOCS[doc], doc=doc, updated=UPDATED)

    @app.post("/lang")
    @limiter.limit("60 per minute")
    def set_language():
        code = request.form.get("code", "")
        target = safe_next(request.form.get("next")) or url_for("index")
        resp = redirect(target)
        if code in LANGS:
            resp.set_cookie("cwas_lang", code, max_age=31536000, samesite="Lax", secure=current_app.config["IS_PROD"])
            if current_user.is_authenticated:
                current_user.language = code
                db.session.commit()
        return resp

    @app.get("/sw.js")
    def service_worker():
        r = send_file(os.path.join(app.static_folder, "js", "sw.js"), mimetype="application/javascript")
        r.headers["Service-Worker-Allowed"] = "/"
        r.headers["Cache-Control"] = "no-cache"
        return r

    @app.get("/manifest.webmanifest")
    def manifest():
        th = g.get("theme", "saina")
        colour = {"saina": "#FFFFFF", "fotsy": "#FFFFFF", "maitso": "#007E3A", "mena": "#D42A20"}.get(th, "#FFFFFF")
        base = f"/static/icons/{th}/"
        return jsonify(name="CWAS - Community Water Access Scheduler", short_name="CWAS", start_url="/app", display="standalone",
                       background_color=colour, theme_color=colour, icons=[{"src": base + "icon-192.png", "sizes": "192x192", "type": "image/png"},
                              {"src": base + "icon-512.png", "sizes": "512x512", "type": "image/png"},
                              {"src": base + "icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}])

    @app.get("/offline")
    def offline():
        return render_template("offline.html")

    # ── auth ────────────────────────────────────────────────────────────────
    def finish_login(user, nxt=None):
        session.clear()
        login_user(user)
        session.permanent = True
        user.last_login_at, user.failed_logins, user.locked_until = utcnow(), 0, None
        S.audit("auth.login", "user", user.id, user.role, actor=user)
        db.session.commit()
        return redirect(safe_next(nxt) or home_for(user))

    @app.route("/login", methods=["GET", "POST"])
    @limiter.limit("10 per minute", methods=["POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(home_for(current_user))
        if request.method == "POST":
            ident = S.clean_text(request.form.get("identifier"), 190).lower()
            pw = request.form.get("password", "")
            phone = S.norm_phone(ident)
            user = User.query.filter(or_(User.email == ident, User.phone == phone if phone else False)).first()
            now = utcnow()
            if user and user.locked_until and user.locked_until > now:
                say("Too many attempts. Try again in 15 minutes.", "err")
            elif user and user.password_hash and check_password_hash(user.password_hash, pw):
                if not user.is_active_flag:
                    say("This account is not active yet. Ask an administrator.", "err")
                elif user.mfa_enabled:
                    session.clear()
                    session["mfa_uid"], session["mfa_next"], session["mfa_tries"] = user.id, request.form.get("next", ""), 0
                    return redirect(url_for("login_mfa"))
                else:
                    return finish_login(user, request.form.get("next"))
            else:
                check_password_hash(DUMMY_HASH, pw)
                if user:
                    user.failed_logins += 1
                    if user.failed_logins >= 5:
                        user.locked_until, user.failed_logins = now + timedelta(minutes=15), 0
                        S.audit("auth.lock", "user", user.id, "5 failed sign-ins", actor=user)
                    db.session.commit()
                say("Wrong email, phone or password.", "err")
        return render_template("auth/login.html", nxt=safe_next(request.args.get("next")) or "")

    @app.route("/login/mfa", methods=["GET", "POST"])
    @limiter.limit("10 per minute", methods=["POST"])
    def login_mfa():
        uid = session.get("mfa_uid")
        user = db.session.get(User, uid) if uid else None
        if not user:
            return redirect(url_for("login"))
        if request.method == "POST":
            session["mfa_tries"] = session.get("mfa_tries", 0) + 1
            if session["mfa_tries"] > 5:
                session.clear()
                say("Too many attempts. Sign in again.", "err")
                return redirect(url_for("login"))
            if S.totp_verify(user.mfa_secret, request.form.get("code", "")):
                return finish_login(user, session.get("mfa_next"))
            say("That code is not valid.", "err")
        return render_template("auth/mfa.html")

    @app.post("/logout")
    def logout():
        if current_user.is_authenticated:
            S.audit("auth.logout", "user", current_user.id)
            db.session.commit()
        logout_user()
        session.clear()
        return redirect(url_for("index"))

    @app.route("/register", methods=["GET", "POST"])
    @limiter.limit("10 per hour", methods=["POST"])
    def register():
        if current_user.is_authenticated:
            return redirect(home_for(current_user))
        f, errors = request.form, []
        if request.method == "POST":
            name, village = S.clean_text(f.get("name"), 120), S.clean_text(f.get("village"), 120)
            phone, email = S.norm_phone(f.get("phone")), S.clean_text(f.get("email"), 190).lower()
            pw, pin = f.get("password", ""), f.get("pin", "").strip()
            recovery = f.get("recovery", "").strip()
            want_coord = f.get("role") == "coordinator"
            if want_coord:
                code = f.get("access_code", "").strip()
                if not code:
                    errors.append("Enter the coordinator access code.")
                elif not hmac.compare_digest(code.encode(), S.get_setting("coord_access").encode()):
                    errors.append("That code is not valid.")
            flags = S.clean_flags(f.getlist("vuln"))
            band = int_arg("distance", 0, 0, 20000)
            home = home_choice(f)
            if not want_coord:
                if band not in [d for d, _ in S.DISTANCE]:
                    errors.append("Choose how far your household is from water.")
                if not pin:
                    errors.append("Create a 4-digit PIN. It lets you use the service on any phone.")
            if not f.get("accept"):
                errors.append("Please accept the Terms, Privacy Policy and Refund Policy to continue.")
            size = int_arg("family_size", 4, 1, 40)
            if len(name) < 2:
                errors.append("Enter your full name.")
            if not phone:
                errors.append("Enter a valid phone number.")
            if email and not S.valid_email(email):
                errors.append("Enter a valid email address.")
            if S.password_error(pw):
                errors.append(S.password_error(pw))
            if pin and not U.PIN_RE.match(pin):
                errors.append("PIN must be 4 digits")
            if pin and not recovery:
                errors.append("Add a 6-digit recovery code to go with your PIN.")
            if recovery and not S.RECOVERY_RE.match(recovery):
                errors.append("Recovery code must be 6 digits")
            if phone and User.query.filter_by(phone=phone).first() or email and User.query.filter_by(email=email).first():
                errors.append("An account with this phone or email already exists.")
            if not errors:
                u = User(role="coordinator" if want_coord else "member", name=name, phone=phone, email=email or None, language=g.lang,
                         password_hash=generate_password_hash(pw), pin_hash=generate_password_hash(pin) if pin else None,
                         recovery_hash=generate_password_hash(recovery) if recovery else None, is_active_flag=not want_coord)
                db.session.add(u)
                db.session.flush()
                if not want_coord:
                    hh = Household(user_id=u.id, name=name, village=village, family_size=size)
                    S.set_needs(hh, flags, band, home)
                    db.session.add(hh)
                S.audit("user.register", "user", u.id, u.role, actor=u)
                for a in User.query.filter_by(role="admin"):
                    if want_coord:
                        S.notify(a, "{name} asked for coordinator access. Approve in Users.", "system", name=name)
                db.session.commit()
                if want_coord:
                    say("Request sent. An administrator will activate your coordinator account.")
                    return redirect(url_for("login"))
                return finish_login(u)
            for e in errors:
                say(e, "err")
        return render_template("auth/register.html", f=f, VULN=S.VULN, DISTANCE=S.DISTANCE, sources=S.operational_sources())

    def send_reset(user, link):
        body = T("CWAS password reset link (valid 30 minutes): {link}", link=link)
        if os.environ.get("SMTP_HOST") and user.email:
            try:
                m = EmailMessage()
                m["Subject"], m["From"], m["To"] = "CWAS password reset", os.environ.get("SMTP_FROM", "no-reply@cwas.mg"), user.email
                m.set_content(body)
                with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587")), timeout=8) as s:
                    s.starttls()
                    if os.environ.get("SMTP_USER"):
                        s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
                    s.send_message(m)
            except Exception:  # noqa: BLE001
                app.logger.exception("reset email failed")
        if user.phone:
            S.send_sms(user.phone, body, user)

    @app.route("/forgot", methods=["GET", "POST"])
    @limiter.limit("5 per hour", methods=["POST"])
    def forgot():
        if request.method == "POST":
            ident = S.clean_text(request.form.get("identifier"), 190).lower()
            phone = S.norm_phone(ident)
            user = User.query.filter(or_(User.email == ident, User.phone == phone if phone else False)).first()
            if user and user.is_active_flag:
                raw = secrets.token_urlsafe(32)
                db.session.add(PasswordReset(user_id=user.id, token_hash=hashlib.sha256(raw.encode()).hexdigest(), expires_at=utcnow() + timedelta(minutes=30)))
                link = url_for("reset_password", token=raw, _external=True)
                send_reset(user, link)
                S.audit("auth.reset_request", "user", user.id, "", actor=user)
                db.session.commit()
                if not current_app.config["IS_PROD"]:
                    flash(link, "dev")  # development convenience only; never shown in production
            say("If that account exists, a reset link is on its way by SMS or email.")
            return redirect(url_for("login"))
        return render_template("auth/forgot.html")

    @app.route("/forgot-pin", methods=["GET", "POST"])
    @limiter.limit("8 per hour", methods=["POST"])
    def forgot_pin():
        """Web twin of the USSD Forgot PIN screen: the recovery code sets a new PIN; without it, ask for a call."""
        if request.method == "POST":
            f = request.form
            phone = S.norm_phone(f.get("phone"))
            user = User.query.filter_by(phone=phone, is_active_flag=True).first() if phone else None
            if f.get("action") == "help":
                if user and user.phone:
                    S.request_pin_help(user, "web")
                    db.session.commit()
                say("If this number has an account, a coordinator will call it to check the details and reset the PIN.")
                return redirect(url_for("forgot_pin"))
            code, pin, pin2, now = f.get("code", "").strip(), f.get("pin", "").strip(), f.get("pin2", "").strip(), utcnow()
            if not U.PIN_RE.match(pin) or pin != pin2:
                say("Enter the same 4-digit PIN twice.", "err")
            elif user and user.pin_locked_until and user.pin_locked_until > now:
                say("Too many wrong PINs. Try again in 15 minutes.", "err")
            elif user and user.recovery_hash and check_password_hash(user.recovery_hash, code):
                S.set_pin(user, pin, channel="web")
                db.session.commit()
                say("PIN changed. Use it next time you dial {dial}.", dial=S.DIAL)
                return redirect(url_for("forgot_pin"))
            else:
                check_password_hash(DUMMY_HASH, code)  # same work either way, so timing does not reveal accounts
                if user:
                    user.pin_failed = (user.pin_failed or 0) + 1
                    if user.pin_failed >= 3:
                        user.pin_locked_until, user.pin_failed = now + timedelta(minutes=15), 0
                        S.audit("user.pin_lock", "user", user.id, "3 wrong recovery codes on the web", actor=user)
                    db.session.commit()
                say("That phone number and recovery code do not match.", "err")
        return render_template("auth/forgot_pin.html")

    @app.route("/reset/<token>", methods=["GET", "POST"])
    @limiter.limit("10 per hour", methods=["POST"])
    def reset_password(token):
        row = PasswordReset.query.filter_by(token_hash=hashlib.sha256(token.encode()).hexdigest()).first()
        if not row or row.used_at or row.expires_at < utcnow():
            say("This reset link is invalid or expired.", "err")
            return redirect(url_for("forgot"))
        if request.method == "POST":
            pw = request.form.get("password", "")
            if S.password_error(pw):
                say(S.password_error(pw), "err")
            else:
                u = db.session.get(User, row.user_id)
                u.password_hash, u.failed_logins, u.locked_until, u.must_change_password = generate_password_hash(pw), 0, None, False
                row.used_at = utcnow()
                S.audit("auth.reset_done", "user", u.id, "", actor=u)
                db.session.commit()
                say("Password updated. Sign in with the new one.")
                return redirect(url_for("login"))
        return render_template("auth/reset.html")

    @app.route("/account/password", methods=["GET", "POST"])
    @login_required
    @limiter.limit("10 per hour", methods=["POST"])
    def change_password():
        if request.method == "POST":
            cur, new = request.form.get("current", ""), request.form.get("password", "")
            if not check_password_hash(current_user.password_hash or DUMMY_HASH, cur):
                say("Current password is wrong.", "err")
            elif S.password_error(new):
                say(S.password_error(new), "err")
            elif cur == new:
                say("Choose a password different from the current one.", "err")
            else:
                current_user.password_hash, current_user.must_change_password = generate_password_hash(new), False
                S.audit("auth.password_change", "user", current_user.id)
                db.session.commit()
                say("Password changed.")
                if current_user.role == "admin" and not current_user.mfa_enabled:
                    say("Add a second sign-in step under Security to protect this account.")
                return redirect(home_for(current_user))
        return render_template("auth/password.html")

    @app.route("/account/security", methods=["GET", "POST"])
    @login_required
    def security():
        action = request.form.get("action")
        if request.method == "POST" and action == "start":
            session["mfa_setup"] = S.new_totp_secret()
        elif request.method == "POST" and action == "enable":
            secret = session.get("mfa_setup")
            if secret and S.totp_verify(secret, request.form.get("code", "")):
                current_user.mfa_secret, current_user.mfa_enabled = secret, True
                session.pop("mfa_setup", None)
                S.audit("auth.mfa_enable", "user", current_user.id)
                db.session.commit()
                say("Two-step sign-in is on.")
            else:
                say("That code is not valid.", "err")
        elif request.method == "POST" and action == "disable":
            if check_password_hash(current_user.password_hash or DUMMY_HASH, request.form.get("password", "")):
                current_user.mfa_enabled, current_user.mfa_secret = False, None
                S.audit("auth.mfa_disable", "user", current_user.id)
                db.session.commit()
                say("Two-step sign-in is off.")
            else:
                say("Current password is wrong.", "err")
        secret, qr = session.get("mfa_setup"), None
        if secret:
            qr = segno.make(S.totp_uri(secret, current_user.email or current_user.phone or "user")).svg_inline(scale=5, dark="#0E2A1B", light="#FFFFFF", border=2)
        return render_template("auth/security.html", secret=secret, qr=qr)

