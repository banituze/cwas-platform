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
                if len(name) < 2 or (email and not S.valid_email(email)):
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

