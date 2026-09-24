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


def external_url(endpoint, **values):
    """An absolute link for an SMS or an email: SITE_URL when set, otherwise this host, always https in production (a proxy in
    front of Railway can make the request look like http), so a phone shows it as a link and opens it securely."""
    base = (os.environ.get("SITE_URL") or request.host_url).rstrip("/")
    if current_app.config["IS_PROD"] and base.startswith("http://"):
        base = "https://" + base[len("http://"):]
    return base + url_for(endpoint, **values)


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
    app.jinja_env.globals["cc_data"] = S.phone_countries  # the country picker's list (templates/partials/phone.html)

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
            phone = S.parse_phone(ident, request.form.get("cc", "MG")) if "@" not in ident else ""
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
            phone, email = S.parse_phone(f.get("phone"), f.get("cc", "MG")), S.clean_text(f.get("email"), 190).lower()
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
                if want_coord:  # a welcome by SMS as soon as the account exists
                    S.event(u, "Welcome {name}! Your coordinator account is waiting for approval.", "system", sms=True, name=S.first_name(name))
                else:
                    S.event(u, "Welcome {name}! Account created. Dial {dial} to book a slot.", "system", sms=True, name=S.first_name(name), dial=S.DIAL)
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
                link = external_url("reset_password", token=raw)
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

    # ── member app ──────────────────────────────────────────────────────────
    def my_household():
        h = current_user.household
        if not h:
            abort(403)
        return h

    @app.get("/app")
    @member_required
    def dashboard():
        h = my_household()
        upcoming = Booking.query.filter(Booking.household_id == h.id, Booking.date >= S.today_local(), Booking.status.in_(("pending", "approved"))).order_by(Booking.date, Booking.start_min).limit(3).all()
        recent = WalletTxn.query.filter_by(household_id=h.id).order_by(WalletTxn.id.desc()).limit(5).all()
        notes = Notification.query.filter_by(user_id=current_user.id).order_by(Notification.id.desc()).limit(4).all()
        score, parts = S.priority_breakdown(h)
        return render_template("member/dashboard.html", h=h, upcoming=upcoming, recent=recent, notes=notes, score=score, sources=S.operational_sources())

    @app.route("/app/book", methods=["GET", "POST"])
    @member_required
    def book():
        h = my_household()
        sources = S.operational_sources()
        alts = []
        if request.method == "POST":
            try:
                day = date.fromisoformat(request.form.get("date", ""))
                b = S.create_booking(h, int_arg("source", 0, 0, 10 ** 9), day, int_arg("slot", -1, -1, 1440), int_arg("litres", 0, 0, 500), "web", current_user)
                db.session.commit()
                say("Booking {ref} sent for approval. {amount} reserved.", ref=b.ref, amount=S.fmt_ar(b.amount))
                return redirect(url_for("booking_detail", ref=b.ref))
            except ValueError:
                db.session.rollback()
                say("Choose a day within the next 7 days.", "err")
            except S.ServiceError as e:
                db.session.rollback()
                say_error(e)
                alts = e.params.get("alts", [])
        want = request.values.get("source") or str(h.home_source_id or "")
        src = next((s for s in sources if str(s.id) == want), sources[0] if sources else None)
        day = parse_date(request.values.get("date"), S.today_local())
        if not (S.today_local() <= day < S.today_local() + timedelta(days=S.HORIZON_DAYS)):
            day = S.today_local()
        slots = S.slot_list(src, day) if src else []
        quotes = {l: S.price_quote(h, src, l)[0] for l in S.litre_options(src)} if src else {}
        return render_template("member/book.html", h=h, sources=sources, src=src, day=day, days=S.booking_days(), slots=slots, quotes=quotes,
                               pct=S.price_quote(h, src, 100)[1] if src else 0, alts=alts)

    @app.get("/api/slots")
    @member_required
    def api_slots():
        src = db.session.get(WaterSource, int_arg("source", 0, 0, 10 ** 9, request.args))
        day = parse_date(request.args.get("date"), S.today_local())
        return jsonify(slots=S.slot_list(src, day) if src else [])

    @app.get("/app/bookings")
    @member_required
    def bookings():
        h = my_household()
        status = request.args.get("status", "")
        q = Booking.query.filter_by(household_id=h.id)
        if status in ("pending", "approved", "denied", "cancelled", "collected", "no_show"):
            q = q.filter_by(status=status)
        page = q.order_by(Booking.date.desc(), Booking.start_min.desc()).paginate(page=int_arg("page", 1, 1, 9999, request.args), per_page=12, error_out=False)
        ids = [b.id for b in page.items]
        paid = {x.booking_id for x in WalletTxn.query.filter(WalletTxn.booking_id.in_(ids), WalletTxn.kind == "booking_debit", WalletTxn.status == "posted")} if ids else set()
        return render_template("member/bookings.html", page=page, flt=status, paid=paid)

    def own_booking(ref):
        b = Booking.query.filter_by(ref=ref).first_or_404()
        if current_user.role == "member" and b.household_id != current_user.household.id:
            abort(404)
        return b

    @app.get("/app/bookings/<ref>")
    @login_required
    def booking_detail(ref):
        b = own_booking(ref)
        debit = WalletTxn.query.filter_by(booking_id=b.id, kind="booking_debit", status="posted").first()
        refund = WalletTxn.query.filter_by(booking_id=b.id, kind="booking_refund", status="posted").first()
        start = datetime.combine(b.date, datetime.min.time()) + timedelta(minutes=b.start_min)
        return render_template("member/booking.html", b=b, debit=debit, refund=refund, can_cancel=b.status in ("pending", "approved") and start > S.now_local())

    @app.post("/app/bookings/<ref>/cancel")
    @member_required
    def booking_cancel(ref):
        b = own_booking(ref)
        try:
            S.cancel_booking(b, current_user)
            db.session.commit()
            say("Booking cancelled. Your money is back in the wallet.")
        except S.ServiceError as e:
            db.session.rollback()
            say_error(e)
        return redirect(url_for("booking_detail", ref=ref))

    @app.get("/app/bookings/<ref>/receipt")
    @login_required
    def receipt(ref):
        b = own_booking(ref)
        debit = WalletTxn.query.filter_by(booking_id=b.id, kind="booking_debit", status="posted").first_or_404()
        if request.args.get("partial"):
            return render_template("member/_receipt.html", b=b, debit=debit, modal=True)
        return render_template("member/receipt.html", b=b, debit=debit, modal=False)

    @app.route("/app/wallet", methods=["GET", "POST"])
    @member_required
    def wallet():
        h = my_household()
        if request.method == "POST":
            amount = request.form.get("amount_custom", "").strip() or request.form.get("amount", "")
            try:
                txn = S.deposit(h, int(amount), request.form.get("provider", ""), current_user, "web")
                db.session.commit()
                say("Deposit {ref} posted. New balance {balance}." if txn.status == "posted" else "Deposit {ref} recorded and waiting for confirmation.",
                    ref=txn.reference, balance=S.fmt_ar(h.balance))
                return redirect(url_for("wallet"))
            except (ValueError, TypeError):
                say("Enter a valid amount.", "err")
            except S.ServiceError as e:
                db.session.rollback()
                say_error(e)
        rows = WalletTxn.query.filter_by(household_id=h.id).order_by(WalletTxn.id.desc()).limit(40).all()
        return render_template("member/wallet.html", h=h, rows=rows, cash=S.get_setting("cash_enabled") == "1",
                               live=S.payment_mode() == "live")

    @app.get("/app/water-points")
    @member_required
    def sources_list():
        return render_template("member/sources.html", sources=WaterSource.query.order_by(WaterSource.name).all(), h=my_household())

    @app.route("/app/notifications", methods=["GET", "POST"])
    @login_required
    def notifications():
        if request.method == "POST":
            Notification.query.filter_by(user_id=current_user.id, is_read=False).update({"is_read": True})
            db.session.commit()
            return redirect(url_for("notifications"))
        page = Notification.query.filter_by(user_id=current_user.id).order_by(Notification.id.desc()).paginate(page=int_arg("page", 1, 1, 9999, request.args), per_page=15, error_out=False)
        return render_template("member/notifications.html", page=page)

    @app.get("/app/notifications/<int:nid>")
    @login_required
    def notification(nid):
        n = Notification.query.filter_by(id=nid, user_id=current_user.id).first_or_404()
        if not n.is_read:
            n.is_read = True
            db.session.commit()
        return render_template("member/notification.html", n=n)

    @app.route("/app/profile", methods=["GET", "POST"])
    @login_required
    def profile():
        h = current_user.household
        if request.method == "POST":
            f, section = request.form, request.form.get("section")
            if section == "profile":
                name = S.clean_text(f.get("name"), 120)
                email = S.clean_text(f.get("email"), 190).lower()
                if len(name) < 2 or (email and not S.valid_email(email)) or (current_user.role == "admin" and not email):
                    say("Check the name and email address.", "err")
                elif email and User.query.filter(User.email == email, User.id != current_user.id).first():
                    say("An account with this phone or email already exists.", "err")
                else:
                    current_user.name, current_user.email = name, email or None
                    if h:
                        h.name, h.village, h.address = name, S.clean_text(f.get("village"), 120), S.clean_text(f.get("address"), 255)
                        h.family_size, h.access_needs = int_arg("family_size", h.family_size, 1, 40), S.clean_text(f.get("access_needs"), 255)
                        band = int_arg("distance", h.distance_m, 0, 20000)
                        S.set_needs(h, f.getlist("vuln"), band if band in [d for d, _ in S.DISTANCE] else h.distance_m, home_choice(f))
                    S.audit("user.profile", "user", current_user.id, "web")
                    db.session.commit()
                    say("Profile saved.")
            elif section in ("pin", "recovery"):
                pin_form = section == "pin"
                new, again = (f.get("pin", ""), f.get("pin2", "")) if pin_form else (f.get("code", ""), f.get("code2", ""))
                if not check_password_hash(current_user.password_hash or DUMMY_HASH, f.get("current", "")):
                    say("Current password is wrong.", "err")
                elif new != again or not (U.PIN_RE if pin_form else S.RECOVERY_RE).match(new):
                    say("Enter the same 4-digit PIN twice." if pin_form else "Enter the same 6-digit recovery code twice.", "err")
                else:
                    (S.set_pin if pin_form else S.set_recovery)(current_user, new, channel="web")
                    db.session.commit()
                    say("PIN changed." if pin_form else "Recovery code saved. Keep it private.")
            elif section == "language" and f.get("language") in LANGS:
                current_user.language = f["language"]
                db.session.commit()
                say("Language saved. It now applies on web and SMS too.")
                resp = redirect(url_for("profile"))
                resp.set_cookie("cwas_lang", f["language"], max_age=31536000, samesite="Lax")
                return resp
            elif section == "pin":
                pin = f.get("pin", "").strip()
                if not U.PIN_RE.match(pin):
                    say("PIN must be 4 digits", "err")
                elif current_user.pin_hash and not check_password_hash(current_user.password_hash or DUMMY_HASH, f.get("password", "")):
                    say("Current password is wrong.", "err")
                else:
                    current_user.pin_hash, current_user.pin_failed, current_user.pin_locked_until = generate_password_hash(pin), 0, None
                    S.audit("user.pin_set", "user", current_user.id, "web")
                    db.session.commit()
                    say("PIN saved. Use it on *384*9411#.")
            return redirect(url_for("profile"))
        score, parts = S.priority_breakdown(h) if h else (0, [])
        return render_template("member/profile.html", h=h, score=score, parts=parts, all_sources=S.operational_sources())


    # ── assistant: saved chats, files, voice-friendly replies ───────────────
    def instance_root():
        return current_app.config["INSTANCE_DIR"]

    def msg_json(m):
        files = []
        for i, f in enumerate(json.loads(m.files or "[]")):
            files.append({"name": f["name"], "kind": f["kind"], "size": UP.human(f["size"]), "url": url_for("chat_file", mid=m.id, idx=i), "image": f["kind"] == "image"})
        return {"id": m.id, "role": m.role, "body": m.body, "files": files, "at": (m.created_at + timedelta(hours=3)).strftime("%d/%m %H:%M")}

    def my_thread(tid):
        t = db.session.get(ChatThread, tid)
        if not t or t.user_id != current_user.id:
            abort(404)
        return t

    @app.get("/app/assistant")
    @member_required
    def assistant():
        threads = ChatThread.query.filter_by(user_id=current_user.id).order_by(ChatThread.updated_at.desc()).all()
        active = next((t for t in threads if str(t.id) == request.args.get("t")), threads[0] if threads else None)
        return render_template("member/assistant.html", threads=[{"id": t.id, "title": t.title} for t in threads],
                               active=active.id if active else 0, messages=[msg_json(m) for m in active.messages] if active else [])

    @app.get("/api/assistant/threads/<int:tid>")
    @member_required
    def api_thread(tid):
        t = my_thread(tid)
        return jsonify(id=t.id, title=t.title, messages=[msg_json(m) for m in t.messages])

    @app.post("/api/assistant/message")
    @member_required
    @limiter.limit("40 per minute")
    def api_message():
        text = S.clean_text(request.form.get("message"), 1000)
        raw = [f for f in request.files.getlist("files") if f and f.filename][:UP.MAX_FILES]
        blobs = []
        for f in raw:
            data = f.read(UP.MAX_BYTES + 1)
            if len(data) > UP.MAX_BYTES:
                return jsonify(error="too_big"), 413
            name = secure_name(f.filename)
            blobs.append((name, UP.optimize(name, data)))
        if not text and not blobs:
            return jsonify(error="empty"), 400
        tid = request.form.get("thread_id", type=int)
        t = my_thread(tid) if tid else ChatThread(user_id=current_user.id, title=(text or blobs[0][0])[:40])
        if not tid:
            db.session.add(t)
            db.session.flush()
        meta = []
        for name, data in blobs:
            kind, mime = UP.detect(name, data)
            meta.append({"fid": UP.save(instance_root(), current_user.id, data), "name": name, "mime": mime, "kind": kind, "size": len(data)})
        um = ChatMessage(thread_id=t.id, role="user", body=text, files=json.dumps(meta))
        reply = UP.reply_for(text, blobs, g.lang) if blobs else ""
        if text and (not blobs or len(text.split()) > 1):
            extra = S.assistant_reply(current_user, text, g.lang)
            reply = (reply + "\n\n" + extra) if reply else extra
        am = ChatMessage(thread_id=t.id, role="assistant", body=reply)
        db.session.add_all([um, am])
        t.updated_at = utcnow()
        db.session.commit()
        return jsonify(thread={"id": t.id, "title": t.title}, user=msg_json(um), assistant=msg_json(am))

    @app.post("/api/assistant/threads/<int:tid>/rename")
    @member_required
    def api_rename(tid):
        t = my_thread(tid)
        t.title = S.clean_text((request.get_json(silent=True) or {}).get("title"), 60) or t.title
        db.session.commit()
        return jsonify(id=t.id, title=t.title)

    def erase_chats(threads):
        for t in threads:
            for m in t.messages:
                for f in json.loads(m.files or "[]"):
                    p = UP.path_of(instance_root(), t.user_id, f["fid"])
                    if p:
                        p.unlink(missing_ok=True)
            db.session.delete(t)

    @app.post("/api/assistant/threads/<int:tid>/delete")
    @member_required
    def api_delete_thread(tid):
        erase_chats([my_thread(tid)])
        db.session.commit()
        return jsonify(ok=True)

    @app.post("/api/assistant/threads/delete-all")
    @member_required
    def api_delete_all():
        erase_chats(ChatThread.query.filter_by(user_id=current_user.id).all())
        db.session.commit()
        return jsonify(ok=True)

    @app.get("/app/assistant/<int:tid>/export.txt")
    @member_required
    def chat_export(tid):
        t = my_thread(tid)
        lines = [f"CWAS assistant: {t.title}", ""]
        for m in t.messages:
            lines.append(f"[{(m.created_at + timedelta(hours=3)):%Y-%m-%d %H:%M}] {'You' if m.role == 'user' else 'CWAS'}: {m.body}")
            for f in json.loads(m.files or "[]"):
                lines.append(f"    (file: {f['name']}, {UP.human(f['size'])})")
        r = make_response("\n".join(lines))
        r.headers["Content-Type"] = "text/plain; charset=utf-8"
        r.headers["Content-Disposition"] = f"attachment; filename=cwas-chat-{t.id}.txt"
        return r

    @app.get("/app/assistant/files/<int:mid>/<int:idx>")
    @member_required
    def chat_file(mid, idx):
        m = db.session.get(ChatMessage, mid) or abort(404)
        if m.thread.user_id != current_user.id:
            abort(404)
        files = json.loads(m.files or "[]")
        if idx >= len(files):
            abort(404)
        f = files[idx]
        p = UP.path_of(instance_root(), current_user.id, f["fid"]) or abort(404)
        inline = f["kind"] in ("image", "pdf")
        r = send_file(p, mimetype=f["mime"] if inline else "application/octet-stream", as_attachment=not inline, download_name=f["name"])
        r.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"
        return r

    def secure_name(name):
        base = re.sub(r"[^\w.\- ]+", "_", os.path.basename(name or "file")).strip(" .") or "file"
        return base[:80]

    # ── live notifications ──────────────────────────────────────────────────
    @app.get("/api/notifications/poll")
    @login_required
    @limiter.limit("60 per minute")
    def api_poll():
        after = int_arg("after", 0, 0, 10 ** 12, request.args)
        rows = Notification.query.filter(Notification.user_id == current_user.id, Notification.id > after).order_by(Notification.id).limit(5).all()
        return jsonify(items=[{"id": n.id, "text": S.render_notification(n, g.lang)[:160], "href": url_for("notification", nid=n.id)} for n in rows],
                       last=rows[-1].id if rows else after, unread=Notification.query.filter_by(user_id=current_user.id, is_read=False).count())

    # ── your data: export and delete ────────────────────────────────────────
    @app.get("/account/export.json")
    @login_required
    def export_data():
        u, h = current_user, current_user.household
        data = {"exported_at": utcnow().isoformat(), "account": {"name": u.name, "email": u.email, "phone": u.phone, "role": u.role, "language": u.language, "created": u.created_at.isoformat() if getattr(u, "created_at", None) else None}}
        if h:
            data["household"] = {"name": h.name, "village": h.village, "address": h.address, "family_size": h.family_size, "access_needs": h.access_needs, "priority": h.priority_level, "balance_mga": h.balance}
            data["bookings"] = [{"ref": b.ref, "date": b.date.isoformat(), "start": S.fmt_min(b.start_min), "water_point": b.source.name, "litres": b.litres, "amount_mga": b.amount, "status": b.status, "channel": b.channel}
                                for b in Booking.query.filter_by(household_id=h.id).order_by(Booking.id)]
            data["wallet"] = [{"reference": t.reference, "kind": t.kind, "provider": t.provider, "status": t.status, "amount_mga": t.amount, "at": t.created_at.isoformat()} for t in WalletTxn.query.filter_by(household_id=h.id).order_by(WalletTxn.id)]
        data["notifications"] = [{"at": n.created_at.isoformat(), "text": S.render_notification(n, g.lang)} for n in Notification.query.filter_by(user_id=u.id).order_by(Notification.id)]
        data["chats"] = [{"title": t.title, "messages": [{"role": m.role, "text": m.body, "files": [f["name"] for f in json.loads(m.files or "[]")]} for m in t.messages]} for t in ChatThread.query.filter_by(user_id=u.id)]
        S.audit("account.export", "user", u.id, "", actor=u)
        db.session.commit()
        r = make_response(json.dumps(data, indent=2, ensure_ascii=False))
        r.headers["Content-Type"] = "application/json; charset=utf-8"
        r.headers["Content-Disposition"] = "attachment; filename=cwas-my-data.json"
        return r

    def erase_account(u, channel="web"):
        uid, phone, name, h = u.id, u.phone, u.name, u.household
        due = 0
        if h:
            for b in Booking.query.filter(Booking.household_id == h.id, Booking.status.in_(("pending", "approved"))).all():
                try:
                    S.cancel_booking(b, u, channel)
                except S.ServiceError:
                    pass  # slot already started: it stays on record
            db.session.flush()
            db.session.refresh(h)
            if h.balance > 0:
                due = h.balance
                S.wallet_post(h, "adjustment", -due, "cash", note="Account closed: refund due in cash", actor=u)
            h.name, h.address, h.access_needs, h.village = "Deleted household", "", "", ""
        if due:
            for st in User.query.filter(User.role.in_(("coordinator", "admin")), User.is_active_flag.is_(True), User.id != uid):
                S.notify(st, "Account closed: {name} ({phone}) is owed {amount} in cash.", "wallet", name=name, phone=phone or "-", amount=S.fmt_ar(due))
        erase_chats(ChatThread.query.filter_by(user_id=uid).all())
        shutil.rmtree(os.path.join(instance_root(), "uploads", str(uid)), ignore_errors=True)
        Notification.query.filter_by(user_id=uid).delete()
        PasswordReset.query.filter_by(user_id=uid).delete()
        if phone:
            UssdSession.query.filter_by(phone=phone).delete()
            SmsLog.query.filter_by(phone=phone).delete()
        S.audit("account.delete", "user", uid, f"self-service, refund due {due} MGA", actor=u, channel=channel)
        u.name, u.email, u.phone, u.password_hash, u.pin_hash = f"Deleted user {uid}", None, None, None, None
        u.mfa_enabled, u.mfa_secret, u.is_active_flag = False, None, False
        return due

    app.extensions["cwas.erase_account"] = erase_account  # USSD "Delete account" runs the same erasure

    @app.route("/account/delete", methods=["GET", "POST"])
    @login_required
    def delete_account():
        u, h = current_user, current_user.household
        bal = h.balance if h else 0
        last_admin = u.role == "admin" and User.query.filter_by(role="admin", is_active_flag=True).count() <= 1
        if request.method == "POST" and not last_admin:
            secret = request.form.get("password", "")
            ok = bool(u.password_hash and check_password_hash(u.password_hash, secret)) or bool(not u.password_hash and u.pin_hash and check_password_hash(u.pin_hash, secret))
            if not ok:
                say("Current password is wrong.", "err")
            elif request.form.get("confirm", "").strip().upper() != "DELETE":
                say("Type DELETE to confirm.", "err")
            elif bal > 0 and not request.form.get("refund"):
                say("Please confirm how your remaining balance will be handled.", "err")
            else:
                erase_account(u)
                db.session.commit()
                logout_user()
                session.clear()
                flash(T("Your account and personal data were deleted."), "ok")
                return redirect(url_for("index"))
        return render_template("account/delete.html", bal=bal, last_admin=last_admin)

    # ── coordinator console ─────────────────────────────────────────────────
    @app.get("/coord")
    @staff_required
    def coord_home():
        t = S.today_local()
        today = Booking.query.filter_by(date=t).all()
        counts = {}
        for b in today:
            counts[b.status] = counts.get(b.status, 0) + 1
        pending = Booking.query.filter_by(status="pending").count()
        pend_dep = WalletTxn.query.filter_by(kind="deposit", status="pending").count()
        sources = WaterSource.query.order_by(WaterSource.name).all()
        load = {s.id: sum(1 for b in today if b.source_id == s.id and b.status in Booking.ACTIVE) for s in sources}
        usage = S.usage_report(t - timedelta(days=6), t)
        week = [(d.strftime("%d/%m"), n) for d, n in usage["by_day"]]
        return render_template("coord/home.html", week=week, counts=counts, pending=pending, pend_dep=pend_dep, sources=sources, load=load, usage=usage,
                               litres=sum(b.litres for b in today if b.status in ("approved", "collected")), flags=S.detect_anomalies()[:4],
                               households=Household.query.count())

    @app.get("/coord/queue")
    @staff_required
    def coord_queue():
        rows = Booking.query.filter_by(status="pending").order_by(Booking.date, Booking.start_min, Booking.priority_score.desc()).all()
        load = {b.id: Booking.query.filter(Booking.source_id == b.source_id, Booking.date == b.date, Booking.start_min == b.start_min, Booking.status.in_(Booking.ACTIVE)).count() for b in rows}
        return render_template("coord/queue.html", rows=rows, load=load)

    @app.post("/coord/queue/<int:bid>")
    @staff_required
    def coord_decide(bid):
        b = db.session.get(Booking, bid) or abort(404)
        try:
            S.decide_booking(b, request.form.get("action") == "approve", request.form.get("note", ""), current_user)
            db.session.commit()
            say("Decision saved and the household was notified.")
        except S.ServiceError as e:
            db.session.rollback()
            say_error(e)
        return redirect(url_for("coord_queue"))

    @app.post("/coord/queue/approve-suggested")
    @staff_required
    def coord_approve_suggested():
        n = 0
        for b in Booking.query.filter_by(status="pending", ai_suggestion="approve").all():
            S.decide_booking(b, True, "Approved with AI suggestion", current_user)
            n += 1
        db.session.commit()
        say("{n} bookings approved.", n=n)
        return redirect(url_for("coord_queue"))

    @app.get("/coord/bookings")
    @staff_required
    def coord_bookings():
        q = Booking.query
        d = parse_date(request.args.get("date"), None)
        st, sid = request.args.get("status", ""), request.args.get("source", "")
        if d:
            q = q.filter_by(date=d)
        if st:
            q = q.filter_by(status=st)
        if sid.isdigit():
            q = q.filter_by(source_id=int(sid))
        page = q.order_by(Booking.date.desc(), Booking.start_min.desc()).paginate(page=int_arg("page", 1, 1, 9999, request.args), per_page=20, error_out=False)
        return render_template("coord/bookings.html", page=page, sources=WaterSource.query.all(), f={"date": d, "status": st, "source": sid})

    @app.post("/coord/bookings/<int:bid>/<action>")
    @staff_required
    def coord_mark(bid, action):
        b = db.session.get(Booking, bid) or abort(404)
        try:
            {"collected": S.mark_collected, "no_show": S.mark_no_show}.get(action, lambda *a: abort(404))(b, current_user)
            db.session.commit()
            say("Saved.")
        except S.ServiceError as e:
            db.session.rollback()
            say_error(e)
        return redirect(request.referrer or url_for("coord_bookings"))

    def read_source_form(src):
        f = request.form
        name = S.clean_text(f.get("name"), 120)
        if len(name) < 2:
            say("Enter a name for the water point.", "err")
            return False
        o, c = S.parse_hhmm(f.get("open"), 360), S.parse_hhmm(f.get("close"), 1080)
        if c <= o:
            say("Closing time must be after opening time.", "err")
            return False
        src.name, src.village = name, S.clean_text(f.get("village"), 120)
        src.kind = f.get("kind") if f.get("kind") in ("borehole", "well", "tap") else "borehole"
        src.status = f.get("status") if f.get("status") in ("operational", "maintenance", "closed") else "operational"
        src.open_min, src.close_min = o, c
        src.slot_minutes, src.slot_capacity = int_arg("slot_minutes", 30, 10, 120), int_arg("slot_capacity", 6, 1, 100)
        src.daily_capacity, src.tariff_per_100l = int_arg("daily_capacity", 80, 1, 5000), int_arg("tariff", 150, 0, 100000)
        src.max_litres = int_arg("max_litres", 100, 20, 100)
        for k, attr in (("latitude", "latitude"), ("longitude", "longitude")):
            try:
                setattr(src, attr, float(f.get(k)) if f.get(k) else None)
            except ValueError:
                setattr(src, attr, None)
        return True

    @app.get("/coord/sources")
    @staff_required
    def coord_sources():
        return render_template("coord/sources.html", sources=WaterSource.query.order_by(WaterSource.name).all())

    @app.route("/coord/sources/new", methods=["GET", "POST"])
    @app.route("/coord/sources/<int:sid>", methods=["GET", "POST"])
    @staff_required
    def coord_source(sid=None):
        src = (db.session.get(WaterSource, sid) or abort(404)) if sid else WaterSource()
        if request.method == "POST":
            if read_source_form(src):
                if not sid:
                    db.session.add(src)
                db.session.flush()
                S.audit("source.save", "source", src.id, f"{src.name} {src.status} {src.tariff_per_100l}Ar/100L")
                db.session.commit()
                say("Water point saved.")
                return redirect(url_for("coord_sources"))
        return render_template("coord/source.html", s=src, new=not sid)

    @app.post("/coord/sources/<int:sid>/delete")
    @staff_required
    def coord_source_delete(sid):
        src = db.session.get(WaterSource, sid) or abort(404)
        if Booking.query.filter_by(source_id=sid).first():
            src.status = "closed"
            say("This water point has booking history, so it was closed instead of deleted.")
        else:
            Maintenance.query.filter_by(source_id=sid).delete()
            db.session.delete(src)
            say("Water point deleted.")
        S.audit("source.delete", "source", sid, src.name)
        db.session.commit()
        return redirect(url_for("coord_sources"))

    @app.route("/coord/maintenance", methods=["GET", "POST"])
    @staff_required
    def coord_maintenance():
        if request.method == "POST":
            try:
                src = db.session.get(WaterSource, int_arg("source", 0, 0, 10 ** 9)) or abort(404)
                start = datetime.fromisoformat(request.form.get("start", ""))
                end = datetime.fromisoformat(request.form.get("end", ""))
                _, n = S.schedule_maintenance(src, start, end, request.form.get("reason", ""), current_user)
                db.session.commit()
                say("Maintenance scheduled. {n} booking(s) were moved and refunded.", n=n)
            except ValueError:
                say("Enter a valid start and end time.", "err")
            except S.ServiceError as e:
                db.session.rollback()
                say_error(e)
            return redirect(url_for("coord_maintenance"))
        rows = Maintenance.query.order_by(Maintenance.starts_at.desc()).limit(40).all()
        return render_template("coord/maintenance.html", rows=rows, sources=WaterSource.query.order_by(WaterSource.name).all(), now=S.now_local())

    @app.post("/coord/maintenance/<int:mid>/cancel")
    @staff_required
    def coord_maintenance_cancel(mid):
        m = db.session.get(Maintenance, mid) or abort(404)
        m.status = "cancelled"
        S.audit("maintenance.cancel", "source", m.source_id, m.reason)
        db.session.commit()
        say("Maintenance cancelled. The slots are open again.")
        return redirect(url_for("coord_maintenance"))

    @app.get("/coord/households")
    @staff_required
    def coord_households():
        q, term = Household.query.join(User), S.clean_text(request.args.get("q"), 60)
        if term:
            q = q.filter(or_(Household.name.ilike(f"%{term}%"), Household.village.ilike(f"%{term}%"), User.phone.ilike(f"%{term}%")))
        page = q.order_by(Household.name).paginate(page=int_arg("page", 1, 1, 9999, request.args), per_page=20, error_out=False)
        return render_template("coord/households.html", page=page, q=term)

    @app.route("/coord/households/<int:hid>", methods=["GET", "POST"])
    @staff_required
    def coord_household(hid):
        h = db.session.get(Household, hid) or abort(404)
        if request.method == "POST":
            f, section = request.form, request.form.get("section")
            if section == "priority":
                h.priority_level = f.get("priority") if f.get("priority") in ("standard", "elevated", "high") else h.priority_level
                h.distance_m, h.access_needs = int_arg("distance", h.distance_m, 0, 20000), S.clean_text(f.get("access_needs"), 255)
                h.vuln_flags, h.needs_review = S.clean_flags(f.getlist("vuln")), False
                S.audit("household.priority", "household", h.id, f"{h.priority_level}; checked by coordinator")
                S.notify(h.user, "Your priority level was set to {level} by the coordinator.", "system", level=h.priority_level)
                db.session.commit()
                say("Saved.")
            elif section == "pin_reset":
                if not f.get("checked"):
                    say("Confirm that you checked the person's details first.", "err")
                elif not (h.user and h.user.phone and h.user.is_active_flag):
                    say("This household has no active phone number.", "err")
                else:
                    S.reset_pin_by_staff(h.user, current_user, "web")
                    db.session.commit()
                    say("PIN reset. A temporary PIN was sent to the household by SMS.")
            elif section == "cash":
                try:
                    txn = S.deposit(h, int(f.get("amount", "0")), "cash", current_user, "staff")
                    db.session.commit()
                    say("Cash deposit {ref} posted.", ref=txn.reference)
                except (ValueError, TypeError):
                    say("Enter a valid amount.", "err")
                except S.ServiceError as e:
                    db.session.rollback()
                    say_error(e)
            return redirect(url_for("coord_household", hid=hid))
        score, parts = S.priority_breakdown(h)
        rows = WalletTxn.query.filter_by(household_id=h.id).order_by(WalletTxn.id.desc()).limit(15).all()
        return render_template("coord/household.html", h=h, score=score, parts=parts, rows=rows, bookings=h.bookings[:10], ok=S.reconcile_wallet(h))

    @app.route("/coord/deposits", methods=["GET", "POST"])
    @staff_required
    def coord_deposits():
        if request.method == "POST":
            txn = db.session.get(WalletTxn, int_arg("id", 0, 0, 10 ** 9)) or abort(404)
            try:
                if request.form.get("action") == "confirm":
                    S.confirm_pending_deposit(txn, current_user)
                else:
                    S.reject_pending_deposit(txn, current_user, "web", request.form.get("reason", ""))
                db.session.commit()
                say("Saved.")
            except S.ServiceError as e:
                db.session.rollback()
                say_error(e)
            return redirect(url_for("coord_deposits"))
        return render_template("coord/deposits.html", rows=WalletTxn.query.filter_by(kind="deposit", status="pending").order_by(WalletTxn.id).all())

    def report_range():
        t = S.today_local()
        d1 = parse_date(request.args.get("to"), t)
        d0 = parse_date(request.args.get("from"), d1 - timedelta(days=29))
        return d0, max(d0, d1)

    @app.get("/coord/reports")
    @staff_required
    def coord_reports():
        d0, d1 = report_range()
        sid = request.args.get("source", "")
        usage = S.usage_report(d0, d1, int(sid) if sid.isdigit() else None)
        st = usage["by_status"]
        done = st.get("collected", 0) + st.get("no_show", 0)
        pct = round(100 * st.get("collected", 0) / done) if done else 0
        by_day = [(d.strftime("%d/%m"), n) for d, n in usage["by_day"][-30:]]
        return render_template("coord/reports.html", by_day=by_day, d0=d0, d1=d1, sid=sid, sources=WaterSource.query.all(),
                               usage=usage, collect_pct=pct, equity=S.equity_report(d0, d1), fin=S.financial_report(d0, d1))

    @app.get("/coord/export/<kind>.csv")
    @staff_required
    def coord_export(kind):
        d0, d1 = report_range()
        if kind == "bookings":
            rows = Booking.query.filter(Booking.date >= d0, Booking.date <= d1).order_by(Booking.date, Booking.start_min).all()
            data = S.to_csv(["ref", "date", "start", "end", "source", "household", "village", "litres", "amount_ar", "discount_pct", "status", "channel"],
                            [[b.ref, b.date, S.fmt_min(b.start_min), S.fmt_min(b.end_min), b.source.name, b.household.name, b.household.village, b.litres, b.amount, b.discount_pct, b.status, b.channel] for b in rows])
        elif kind == "wallet":
            rows = WalletTxn.query.filter(WalletTxn.created_at >= datetime.combine(d0, datetime.min.time()) - timedelta(hours=3), WalletTxn.created_at < datetime.combine(d1 + timedelta(days=1), datetime.min.time()) - timedelta(hours=3)).order_by(WalletTxn.id).all()
            data = S.to_csv(["reference", "created_utc", "household", "kind", "provider", "status", "amount_ar", "balance_after"],
                            [[x.reference, x.created_at.isoformat(), x.household.name, x.kind, x.provider, x.status, x.amount, x.balance_after] for x in rows])
        elif kind == "equity":
            data = S.to_csv(["priority", "households", "people", "bookings", "litres", "litres_per_person_per_day"],
                            [[r["level"], r["households"], r["people"], r["bookings"], r["litres"], r["lpcd"]] for r in S.equity_report(d0, d1)])
        elif kind == "households":
            data = S.to_csv(["name", "village", "phone", "family_size", "priority", "distance_m", "balance_ar"],
                            [[h.name, h.village, h.user.phone or "", h.family_size, h.priority_level, h.distance_m, h.balance] for h in Household.query.order_by(Household.name)])
        else:
            abort(404)
        S.audit("report.export", "report", kind, f"{d0}..{d1}")
        db.session.commit()
        r = make_response(data)
        r.headers["Content-Type"] = "text/csv; charset=utf-8"
        r.headers["Content-Disposition"] = f"attachment; filename=cwas-{kind}-{d0}-{d1}.csv"
        return r

    @app.get("/coord/insights")
    @staff_required
    def coord_insights():
        fc = []
        for s in WaterSource.query.filter_by(status="operational").order_by(WaterSource.name).all():
            out, peak = S.forecast(s)
            fc.append((s, [(d["date"].strftime("%a"), d["expected"]) for d in out], peak))
        fairness, flagged = S.fairness_check()
        ranked = sorted(((S.priority_breakdown(h), h) for h in Household.query.all()), key=lambda x: -x[0][0])[:8]
        return render_template("coord/insights.html", forecasts=fc, flags=S.detect_anomalies(), fairness=fairness, flagged=flagged, ranked=ranked)

    @app.route("/coord/announce", methods=["GET", "POST"])
    @staff_required
    def coord_announce():
        if request.method == "POST":
            msg = S.clean_text(request.form.get("message"), 300)
            if len(msg) < 3:
                say("Write a message first.", "err")
            else:
                n = U.broadcast(msg, current_user, "web")
                db.session.commit()
                say("Sent to {n} households.", n=n)
                return redirect(url_for("coord_announce"))
        return render_template("coord/announce.html")

    # ── admin console ───────────────────────────────────────────────────────
    @app.get("/admin")
    @admin_required
    def admin_home():
        ok, bad, n = S.verify_audit_chain()
        return render_template("admin/home.html", users=User.query.count(), households=Household.query.count(), waiting=User.query.filter_by(is_active_flag=False).count(),
                               bookings=Booking.query.count(), chain=(ok, bad, n), sessions=UssdSession.query.count(), sms=SmsLog.query.count(),
                               held=db.session.query(db.func.coalesce(db.func.sum(Household.balance), 0)).scalar(), db_kind=current_app.config["DB_KIND"],
                               recent=AuditLog.query.order_by(AuditLog.id.desc()).limit(6).all(), followers=PilotFollower.query.count())

    @app.post("/pilot/follow")
    @limiter.limit("6 per hour")
    def pilot_follow():
        email = S.clean_text(request.form.get("email"), 190).lower()
        if not S.valid_email(email):
            say("Enter a valid email address.", "err")
        else:
            if not PilotFollower.query.filter_by(email=email).first():
                db.session.add(PilotFollower(email=email, language=g.lang, token=secrets.token_urlsafe(24)))
                S.audit("pilot.follow", "pilot", email.split("@")[-1], channel="web")
                db.session.commit()
            say("Thank you. We will write only when the pilot reaches a milestone.")
        nxt = request.form.get("next") or "/"
        return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")

    @app.get("/pilot/leave/<token>")
    def pilot_leave(token):
        row = PilotFollower.query.filter_by(token=token).first()
        if row:
            db.session.delete(row)
            db.session.commit()
        say("Your email address was removed from pilot news.")
        return redirect(url_for("index"))

    @app.get("/admin/pilot-followers.csv")
    @admin_required
    def admin_pilot_csv():
        rows = PilotFollower.query.order_by(PilotFollower.id).all()
        data = S.to_csv(["email", "language", "joined", "leave_link"],
                        [[r.email, r.language, r.created_at.strftime("%Y-%m-%d"), url_for("pilot_leave", token=r.token, _external=True)] for r in rows])
        S.audit("pilot.export", "pilot", str(len(rows)))
        db.session.commit()
        return send_file(__import__("io").BytesIO(data.encode("utf-8")), mimetype="text/csv", as_attachment=True, download_name="pilot-followers.csv")

    @app.get("/admin/users")
    @admin_required
    def admin_users():
        q, term, role = User.query, S.clean_text(request.args.get("q"), 60), request.args.get("role", "")
        if term:
            q = q.filter(or_(User.name.ilike(f"%{term}%"), User.email.ilike(f"%{term}%"), User.phone.ilike(f"%{term}%")))
        if role in ("member", "coordinator", "admin"):
            q = q.filter_by(role=role)
        page = q.order_by(User.is_active_flag, User.id.desc()).paginate(page=int_arg("page", 1, 1, 9999, request.args), per_page=20, error_out=False)
        return render_template("admin/users.html", page=page, q=term, role=role)

    @app.post("/admin/users/create")
    @admin_required
    def admin_user_create():
        f = request.form
        name, email, phone = S.clean_text(f.get("name"), 120), S.clean_text(f.get("email"), 190).lower(), S.norm_phone(f.get("phone"))
        role, pw = f.get("role"), f.get("password", "")
        if role not in ("member", "coordinator", "admin") or len(name) < 2 or (email and not S.valid_email(email)) or not (email or phone):
            say("Check the name, role and email or phone.", "err")
        elif S.password_error(pw):
            say(S.password_error(pw), "err")
        elif (email and User.query.filter_by(email=email).first()) or (phone and User.query.filter_by(phone=phone).first()):
            say("An account with this phone or email already exists.", "err")
        else:
            u = User(role=role, name=name, email=email or None, phone=phone or None, password_hash=generate_password_hash(pw), must_change_password=True, language=g.lang)
            db.session.add(u)
            db.session.flush()
            if role == "member":
                db.session.add(Household(user_id=u.id, name=name, village=S.clean_text(f.get("village"), 120)))
            S.audit("user.create", "user", u.id, role)
            db.session.commit()
            say("Account created. The person must change the password at first sign-in.")
        return redirect(url_for("admin_users"))

    @app.post("/admin/users/<int:uid>/<action>")
    @admin_required
    def admin_user_action(uid, action):
        u = db.session.get(User, uid) or abort(404)
        admins = User.query.filter_by(role="admin", is_active_flag=True).count()
        if u.id == current_user.id and action in ("deactivate", "delete", "role"):
            say("You cannot do that to your own account.", "err")
        elif action == "activate":
            u.is_active_flag = True
            S.audit("user.activate", "user", u.id)
            S.notify(u, "Your account is now active.", "system")
            say("Account activated.")
        elif action == "deactivate":
            if u.role == "admin" and admins <= 1:
                say("At least one active administrator is required.", "err")
            else:
                u.is_active_flag = False
                S.audit("user.deactivate", "user", u.id)
                say("Account deactivated.")
        elif action == "delete":
            if u.role == "admin" and admins <= 1:
                say("At least one active administrator is required.", "err")
            elif u.household and (Booking.query.filter_by(household_id=u.household.id).first() or WalletTxn.query.filter_by(household_id=u.household.id).first()):
                u.is_active_flag = False
                S.audit("user.deactivate", "user", u.id, "delete refused: has records")
                say("This person has bookings or money records, so the account was deactivated instead of deleted.")
            else:
                Notification.query.filter_by(user_id=u.id).delete()
                S.audit("user.delete", "user", u.id, u.email or u.phone or u.name)
                db.session.delete(u)
                say("Account deleted.")
        elif action == "reset":
            temp = "Cw" + secrets.token_urlsafe(9) + "#7"
            u.password_hash, u.must_change_password, u.failed_logins, u.locked_until = generate_password_hash(temp), True, 0, None
            u.pin_failed, u.pin_locked_until = 0, None
            S.audit("user.reset_password", "user", u.id)
            flash(T("Temporary password for {name}: {pw} (shown once).", name=u.name, pw=temp), "dev")
        elif action == "role" and request.form.get("role") in ("member", "coordinator", "admin"):
            u.role = request.form["role"]
            if u.role != "member":  # the member welcome (dial to book a slot) no longer applies
                Notification.query.filter(Notification.user_id == u.id, Notification.key.like("Welcome {name}! Account created.%")).delete(synchronize_session=False)
            S.notify(u, {"member": "Your account is now a household account.", "coordinator": "Your account is now a coordinator account.",
                         "admin": "Your account is now an administrator account."}[u.role], "system")
            if u.role == "member" and not u.household:
                db.session.add(Household(user_id=u.id, name=u.name))
            S.audit("user.role", "user", u.id, u.role)
            say("Role updated.")
        else:
            abort(404)
        db.session.commit()
        return redirect(request.referrer or url_for("admin_users"))

    SETTING_KEYS = ("discount_elevated", "discount_high", "min_deposit", "max_deposit", "auto_approve", "cash_enabled", "no_show_grace_min", "enroll_coord", "coord_access")
    SECRET_SETTINGS = ("enroll_coord", "coord_access")  # never written to the audit log

    @app.route("/admin/settings", methods=["GET", "POST"])
    @admin_required
    def admin_settings():
        if request.method == "POST":
            for k in SETTING_KEYS:
                if k in ("auto_approve", "cash_enabled"):
                    S.set_setting(k, "1" if request.form.get(k) else "0")
                elif k in SECRET_SETTINGS:
                    code = S.clean_text(request.form.get(k), 40)
                    if len(code) >= 8 and " " not in code:
                        S.set_setting(k, code)
                else:
                    S.set_setting(k, int_arg(k, S.get_int(k), 0, 10 ** 7))
            if S.get_int("discount_high") > 100 or S.get_int("discount_elevated") > 100:
                db.session.rollback()
                say("Discounts cannot be above 100%.", "err")
            else:
                S.audit("settings.save", "settings", "", ", ".join(f"{k}={'(hidden)' if k in SECRET_SETTINGS else S.get_setting(k)}" for k in SETTING_KEYS))
                db.session.commit()
                say("Settings saved.")
            return redirect(url_for("admin_settings"))
        return render_template("admin/settings.html", v={k: S.get_setting(k) for k in SETTING_KEYS}, live=S.payment_mode(),
                               sms=os.environ.get("SMS_ENABLED", "0") == "1", token=bool(os.environ.get("AT_WEBHOOK_TOKEN")))

    @app.get("/admin/audit")
    @admin_required
    def admin_audit():
        q, act = AuditLog.query, S.clean_text(request.args.get("action"), 40)
        if act:
            q = q.filter(AuditLog.action.like(f"{act}%"))
        page = q.order_by(AuditLog.id.desc()).paginate(page=int_arg("page", 1, 1, 99999, request.args), per_page=30, error_out=False)
        return render_template("admin/audit.html", page=page, act=act, chain=S.verify_audit_chain() if request.args.get("verify") else None)

    @app.get("/admin/audit.csv")
    @admin_required
    def admin_audit_csv():
        rows = AuditLog.query.order_by(AuditLog.id).all()
        r = make_response(S.to_csv(["id", "at_utc", "actor", "channel", "action", "entity", "entity_id", "detail", "ip", "hash"],
                                   [[x.id, x.at.isoformat(), x.actor_label, x.channel, x.action, x.entity, x.entity_id, x.detail, x.ip, x.hash] for x in rows]))
        r.headers["Content-Type"] = "text/csv; charset=utf-8"
        r.headers["Content-Disposition"] = "attachment; filename=cwas-audit.csv"
        return r

    TABLES = [User, Household, WaterSource, Booking, WalletTxn, Notification, Maintenance, AuditLog, Setting, UssdSession, SmsLog, PasswordReset, ChatThread, ChatMessage]

    def dump_db():
        out = {"version": 1, "created": utcnow().isoformat(), "tables": {}}
        for m in TABLES:
            rows = []
            for r in db.session.query(m).all():
                rows.append({c.name: (getattr(r, c.key) if hasattr(r, c.key) else getattr(r, c.name)) for c in m.__table__.columns})
            out["tables"][m.__tablename__] = json.loads(json.dumps(rows, default=lambda o: o.isoformat()))
        return out

    def restore_db(data):
        from sqlalchemy import Date, DateTime
        for m in reversed(TABLES):
            db.session.query(m).delete()
        db.session.flush()
        for m in TABLES:
            for row in data["tables"].get(m.__tablename__, []):
                vals = {}
                for c in m.__table__.columns:
                    v = row.get(c.name)
                    if v is not None and isinstance(c.type, DateTime):
                        v = datetime.fromisoformat(v)
                    elif v is not None and isinstance(c.type, Date):
                        v = date.fromisoformat(v)
                    vals[c.name] = v
                db.session.execute(m.__table__.insert().values(**vals))
        db.session.flush()

    @app.route("/admin/database", methods=["GET", "POST"])
    @admin_required
    def admin_database():
        if request.method == "POST":
            action = request.form.get("action")
            if action == "backup":
                name = f"cwas-backup-{utcnow():%Y%m%d-%H%M%S}.json"
                (BACKUP_DIR / name).write_text(json.dumps(dump_db()))
                S.audit("db.backup", "database", name)
                db.session.commit()
                say("Backup {name} created.", name=name)
            elif action == "restore":
                name = os.path.basename(request.form.get("file", ""))
                path = BACKUP_DIR / name
                if request.form.get("confirm") != "RESTORE" or not path.is_file():
                    say("Type RESTORE to confirm and choose a backup.", "err")
                else:
                    try:
                        (BACKUP_DIR / f"cwas-prerestore-{utcnow():%Y%m%d-%H%M%S}.json").write_text(json.dumps(dump_db()))
                        restore_db(json.loads(path.read_text()))
                        S.audit("db.restore", "database", name)
                        db.session.commit()
                        say("Restored from {name}.", name=name)
                    except Exception:  # noqa: BLE001
                        db.session.rollback()
                        app.logger.exception("restore failed")
                        say("Restore failed and nothing was changed.", "err")
            return redirect(url_for("admin_database"))
        return render_template("admin/database.html", files=sorted(BACKUP_DIR.glob("cwas-*.json"), reverse=True)[:20], checks=integrity_checks(), kind=current_app.config["DB_KIND"])

    def integrity_checks():
        out = []
        try:
            if current_app.config["DB_KIND"] == "sqlite":
                out.append(("Storage engine check", db.session.execute(text("PRAGMA integrity_check")).scalar() == "ok"))
            else:
                out.append(("Storage engine check", db.session.execute(text("SELECT 1")).scalar() == 1))
        except Exception:  # noqa: BLE001
            out.append(("Storage engine check", False))
        ok, bad, n = S.verify_audit_chain()
        out.append(("Audit chain ({n} entries)".format(n=n), ok))
        out.append(("Wallet balances match the ledger", all(S.reconcile_wallet(h) for h in Household.query.all())))
        out.append(("No booking without a household or source", db.session.query(Booking).outerjoin(Household).outerjoin(WaterSource).filter(or_(Household.id.is_(None), WaterSource.id.is_(None))).count() == 0))
        return out

    @app.get("/admin/backups/<name>")
    @admin_required
    def admin_backup_download(name):
        path = BACKUP_DIR / os.path.basename(name)
        if not path.is_file():
            abort(404)
        return send_file(path, as_attachment=True)

    @app.get("/admin/channels")
    @admin_required
    def admin_channels():
        return render_template("admin/channels.html", sessions=UssdSession.query.order_by(UssdSession.updated_at.desc()).limit(30).all(),
                               sms=SmsLog.query.order_by(SmsLog.id.desc()).limit(30).all())

    # ── telco webhooks (Africa's Talking) ───────────────────────────────────
    def telco_guard():
        """Telco callbacks name the caller's phone number, so an unsigned one could act as any household. In production the
        line stays closed until AT_WEBHOOK_TOKEN is set; locally (no token) it stays open for testing."""
        want = os.environ.get("AT_WEBHOOK_TOKEN")
        if not want:
            if current_app.config["IS_PROD"]:
                current_app.logger.error("AT_WEBHOOK_TOKEN is not set: telco callbacks are refused until it is")
                abort(503)
            return
        if not hmac.compare_digest(request.args.get("token", ""), want):
            abort(403)

    @app.route("/ussd", methods=["POST", "GET"])
    @app.route("/api/ussd", methods=["POST", "GET"])
    @app.route("/webhooks/ussd", methods=["POST", "GET"])
    @csrf.exempt
    @limiter.limit("1200 per minute")
    def ussd_callback():
        telco_guard()
        f = request.values
        out = U.handle_ussd(S.clean_text(f.get("sessionId"), 80) or "none", f.get("phoneNumber", ""), f.get("text", ""), "telco")
        return out, 200, {"Content-Type": "text/plain; charset=utf-8"}

    @app.route("/sms/incoming", methods=["POST", "GET"])
    @app.route("/api/sms/inbound", methods=["POST", "GET"])
    @csrf.exempt
    @limiter.limit("600 per minute")
    def sms_incoming():
        telco_guard()
        f = request.values
        phone = S.norm_phone(f.get("from", ""))
        reply = U.handle_sms(phone, f.get("text", ""))
        if reply:
            S.send_sms(phone, reply)
        db.session.commit()
        return "OK", 200, {"Content-Type": "text/plain; charset=utf-8"}

    @app.route("/sms/delivery", methods=["POST", "GET"])
    @app.route("/api/sms/delivery", methods=["POST", "GET"])
    @csrf.exempt
    def sms_delivery():
        telco_guard()
        return "OK", 200, {"Content-Type": "text/plain; charset=utf-8"}

    @app.post("/webhooks/payments/<provider>")
    @csrf.exempt
    def payment_webhook(provider):
        """Mobile-money adapters confirm a pending deposit here. Without a shared secret nothing is ever confirmed."""
        secret = os.environ.get("PAYMENT_WEBHOOK_SECRET", "")
        if not secret:
            return jsonify(error="not configured"), 503
        sig = hmac.new(secret.encode(), request.get_data(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, request.headers.get("X-Signature", "")):
            return jsonify(error="bad signature"), 403
        try:
            body = json.loads(request.get_data() or b"{}")
        except ValueError:
            body = {}
        txn = WalletTxn.query.filter_by(reference=str(body.get("reference", "")), provider=provider, status="pending").first()
        if not txn:
            return jsonify(error="unknown reference"), 404
        # the provider must confirm exactly the amount that was requested; anything else is refused and recorded
        if body.get("status") == "success" and body.get("amount") is not None:
            try:
                paid = int(round(float(body.get("amount"))))
            except (TypeError, ValueError):
                return jsonify(error="bad amount"), 400
            if paid != abs(int(txn.amount)):
                S.audit("wallet.mismatch", "wallet", txn.reference, f"{provider} confirmed {paid}, expected {abs(int(txn.amount))}", channel="system")
                db.session.commit()
                return jsonify(error="amount mismatch"), 409
        if body.get("status") == "success":
            S.confirm_pending_deposit(txn, None, "system")
        else:
            txn.status = "failed"
            S.audit("wallet.failed", "wallet", txn.reference, provider, channel="system")
        db.session.commit()
        return jsonify(ok=True)

    # ── simulator ───────────────────────────────────────────────────────────
    def demo_households():
        """The seeded demo households, oldest first. The coordinator has its own button and PIN, so it is not one of them."""
        return User.query.filter(User.phone.like("+2613400%"), User.role == "member").order_by(User.id).limit(3).all()

    def sim_allowed(phone):
        if current_app.config["SIMULATOR_PUBLIC"]:
            return True
        if not current_user.is_authenticated:
            return False
        return current_user.is_staff or S.norm_phone(phone) == current_user.phone

    @app.get("/simulator")
    def simulator():
        if not current_app.config["SIMULATOR_PUBLIC"] and not current_user.is_authenticated:
            return redirect(url_for("login", next=url_for("simulator")))
        demo = demo_households() if current_app.config["DEMO_DATA"] else []
        return render_template("simulator.html", own=current_user.phone if current_user.is_authenticated else "", demo=demo, prod=current_app.config["IS_PROD"])

    @app.get("/simulator/api/tour/<kind>")
    @limiter.limit("30 per minute")
    def sim_tour(kind):
        """A guided run for the device lab, planned against the live data so it can finish every time: a household with a
        free day and the money to book, and the coordinator's approval only when something waits. Only while demo data is on (SEED_DEMO)."""
        if not current_app.config["SIMULATOR_PUBLIC"] and not current_user.is_authenticated:
            abort(403)
        homes = demo_households() if current_app.config["DEMO_DATA"] else []
        if kind not in ("register", "deposit", "book", "approve") or not homes:
            abort(404)
        if kind == "register":
            phone = next((p for p in ("+2613400009%03d" % (100 + secrets.randbelow(900)) for _ in range(40))
                          if not User.query.filter_by(phone=p).first()), None)
            if not phone:
                abort(503)
            return jsonify(phone=phone, steps=["2", "1", "Winebald", "Ampotaka", "5", "0", "2", "1", "1234", "1234", "246810", "246810"])
        if kind == "deposit":
            return jsonify(phone=homes[0].phone, steps=["2", "1", "1", "3", "1", "1234"])
        if kind == "approve":
            coord = User.query.filter(User.phone.like("+2613400%"), User.role == "coordinator").order_by(User.id).first()
            if not coord:
                abort(404)
            waiting = Booking.query.filter_by(status="pending").first()
            return jsonify(phone=coord.phone, steps=["2", "2", "1", "1", "2468"] if waiting else ["2", "1"])
        srcs, days = S.operational_sources()[:5], S.booking_days()
        for d_i in range(1, len(days)):  # from tomorrow: some of today's slots may be over
            for u in homes:
                h = u.household
                if not h or Booking.query.filter(Booking.household_id == h.id, Booking.date == days[d_i], Booking.status.in_(Booking.ACTIVE)).first():
                    continue
                for s_i, src in enumerate(srcs):
                    opts = S.litre_options(src)
                    if opts and h.balance >= S.price_quote(h, src, opts[0])[0] and S.slot_list(src, days[d_i], only_open=True):
                        return jsonify(phone=u.phone, steps=["2", "2", str(s_i + 1), str(d_i + 1), "1", "1", "1", "1234"])
        return jsonify(phone=homes[0].phone, steps=["2", "2", "1", "2", "1", "1", "1", "1234"])

    @app.post("/simulator/api/ussd")
    @limiter.limit("90 per minute")
    def sim_ussd():
        d = request.get_json(silent=True) or {}
        phone = S.norm_phone(d.get("phone", ""))
        if not phone or not sim_allowed(phone):
            return jsonify(error="phone"), 403
        sid = "SIM-" + hashlib.sha256((str(d.get("session", "")) + phone).encode()).hexdigest()[:24]
        out = U.handle_ussd(sid, phone, str(d.get("text", ""))[:200], "simulator")
        return jsonify(response=out, ended=out.startswith("END"), screen=out[4:])

    @app.post("/simulator/api/sms")
    @limiter.limit("60 per minute")
    def sim_sms():
        d = request.get_json(silent=True) or {}
        phone = S.norm_phone(d.get("phone", ""))
        if not phone or not sim_allowed(phone):
            return jsonify(error="phone"), 403
        before = db.session.query(db.func.coalesce(db.func.max(SmsLog.id), 0)).scalar()
        reply = U.handle_sms(phone, str(d.get("text", ""))[:200])
        if reply:
            S.send_sms(phone, reply, live=False)
        db.session.commit()
        rows = SmsLog.query.filter(SmsLog.phone == phone, SmsLog.direction == "out", SmsLog.id > before).order_by(SmsLog.id).all()
        return jsonify(reply=reply, messages=[{"id": r.id, "body": r.body, "at": (r.created_at + timedelta(hours=3)).strftime("%H:%M")} for r in rows],
                       last=db.session.query(db.func.coalesce(db.func.max(SmsLog.id), 0)).scalar())

    @app.get("/simulator/api/inbox")
    @limiter.limit("60 per minute")
    def sim_inbox():
        phone = S.norm_phone(request.args.get("phone", ""))
        if not phone or not sim_allowed(phone):
            return jsonify(error="phone"), 403
        after = int_arg("after", 0, 0, 10 ** 12, request.args)
        rows = SmsLog.query.filter(SmsLog.phone == phone, SmsLog.direction == "out", SmsLog.id > after).order_by(SmsLog.id).limit(30).all()
        return jsonify(messages=[{"id": r.id, "body": r.body, "at": (r.created_at + timedelta(hours=3)).strftime("%H:%M")} for r in rows],
                       last=db.session.query(db.func.coalesce(db.func.max(SmsLog.id), 0)).scalar())
