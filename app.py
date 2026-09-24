"""CWAS - Community Water Access Scheduler.

Run:  pip install -r requirements.txt  &&  python3 app.py
SQLite is the default database. Set DATABASE_URL to a PostgreSQL URL and CWAS uses it; if PostgreSQL cannot be
reached at start-up it falls back to SQLite and says so in the log, so a pilot never goes down for a database URL.
"""
import barcode128
import gzip
import re
import hashlib
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, g, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from flask_wtf.csrf import CSRFError
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from werkzeug.middleware.proxy_fix import ProxyFix
from markupsafe import escape
from werkzeug.security import generate_password_hash

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

from extensions import WRITE_LOCK, csrf, limiter, login_manager  # noqa: E402
from i18n import LANGS, THEMES, negotiate_language, tt  # noqa: E402
from models import Notification, Setting, User, WaterSource, db  # noqa: E402
import services as S  # noqa: E402
import ussd as U  # noqa: E402

log = logging.getLogger("cwas")
IS_PROD = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("CWAS_ENV") == "production")
# Content Security Policy: scripts, styles, fonts and data come only from this site; nothing may frame it. data: covers
# inline SVG icons, blob: covers files the assistant previews before upload. The one outside host is for the two stock
# clips still listed in templates/partials/media.html; drop it once those clips are replaced with local files.
CSP = ("default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob: https://videos.pexels.com; "
       "style-src 'self' 'unsafe-inline'; script-src 'self'; font-src 'self'; connect-src 'self'; manifest-src 'self'; "
       "worker-src 'self'; frame-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'"
       + ("; upgrade-insecure-requests" if IS_PROD else ""))
# anything under these paths holds personal data, balances or controls, so it is never cached and never indexed
PRIVATE_PREFIXES = ("/app", "/coord", "/admin", "/account", "/api/", "/simulator/api/", "/webhooks/", "/ussd", "/sms/")
# public pages worth showing in search results
INDEXABLE = {"index", "platform", "access", "water_points", "about", "legal", "register", "simulator"}
OG_LOCALE = {"en": "en_GB", "fr": "fr_FR", "mg": "mg_MG"}


def _secret_key(instance):
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    f = instance / "secret.key"
    if not f.exists():
        f.write_text(secrets.token_hex(32))
    return f.read_text().strip()


def _database(instance):
    """PostgreSQL when it answers, SQLite otherwise."""
    sqlite_uri = f"sqlite:///{instance / 'cwas.db'}"
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return sqlite_uri, "sqlite"
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    try:
        eng = create_engine(url, connect_args={"connect_timeout": 5})
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        eng.dispose()
        return url, "postgresql"
    except Exception as exc:  # noqa: BLE001
        log.warning("PostgreSQL not reachable (%s). Falling back to SQLite.", exc.__class__.__name__)
        return sqlite_uri, "sqlite"


_GZ_TYPES = {"text/css", "text/javascript", "application/javascript", "image/svg+xml", "application/json", "application/manifest+json"}
_GZ = {}


def _compress(resp):
    """Gzip a text file (CSS, JavaScript, SVG, JSON) once and keep the result: the stylesheet and scripts hold back the first
    paint. Pages themselves stay uncompressed, so a secret on a page can never leak through compression (BREACH)."""
    if (resp.status_code != 200 or resp.mimetype not in _GZ_TYPES or "Content-Encoding" in resp.headers
            or "gzip" not in request.headers.get("Accept-Encoding", "") or request.headers.get("Range")):
        return
    resp.direct_passthrough = False
    raw = resp.get_data()
    key = (request.path, hashlib.sha1(raw).hexdigest())
    if key not in _GZ:
        _GZ[key] = gzip.compress(raw, 9)
    if len(_GZ[key]) >= len(raw):
        return
    resp.set_data(_GZ[key])
    resp.headers["Content-Encoding"] = "gzip"
    resp.headers.pop("Accept-Ranges", None)
    resp.vary.add("Accept-Encoding")
    etag = resp.headers.get("ETag")
    if etag and not etag.startswith("W/"):
        resp.headers["ETag"] = "W/" + etag


def _asset_version(static_dir):
    """Hash of every CSS/JS file: pages link assets as ?v=<hash>, so a browser (or the service worker) can never serve a stale copy."""
    h = hashlib.sha1()
    for f in sorted(Path(static_dir).rglob("*")):
        if f.suffix in (".css", ".js"):
            h.update(f.name.encode() + f.read_bytes())
    return h.hexdigest()[:10]


def _last_note():
    if not current_user.is_authenticated:
        return 0
    return db.session.query(db.func.coalesce(db.func.max(Notification.id), 0)).filter(Notification.user_id == current_user.id).scalar()


def _unread_count():
    if not current_user.is_authenticated:
        return 0
    return Notification.query.filter_by(user_id=current_user.id, is_read=False).count()


def create_app(test_config=None):
    app = Flask(__name__, static_folder="static", template_folder="templates")
    instance = Path(os.environ.get("CWAS_INSTANCE") or BASE / "instance")
    instance.mkdir(exist_ok=True)
    uri, kind = _database(instance)
    app.config.update(
        SECRET_KEY=_secret_key(instance), SQLALCHEMY_DATABASE_URI=uri, DB_KIND=kind,
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True, **({"connect_args": {"timeout": 20}} if kind == "sqlite" else {})},
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=IS_PROD,
        REMEMBER_COOKIE_HTTPONLY=True, REMEMBER_COOKIE_SECURE=IS_PROD, REMEMBER_COOKIE_SAMESITE="Lax", REMEMBER_COOKIE_DURATION=timedelta(days=30),
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),  # sliding: every visit renews it
        SESSION_COOKIE_NAME="__Host-cwas" if IS_PROD else "session",  # __Host-: sent over HTTPS only, to this exact host only
        WTF_CSRF_TIME_LIMIT=None, MAX_CONTENT_LENGTH=40 * 1024 * 1024, JSON_SORT_KEYS=False, IS_PROD=IS_PROD, INSTANCE_DIR=str(instance),
        RATELIMIT_ENABLED=os.environ.get("CWAS_NO_LIMITS") != "1",
        SIMULATOR_PUBLIC=os.environ.get("SIMULATOR_PUBLIC", "0" if IS_PROD else "1") == "1",
        DEMO_DATA=os.environ.get("SEED_DEMO", "0" if IS_PROD else "1") == "1",  # demo households and guided runs in the device lab
    )
    if test_config:
        app.config.update(test_config)
    if IS_PROD:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # Railway terminates TLS at the edge
    db.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "login"
    login_manager.session_protection = "strong"

    if kind == "sqlite":
        @event.listens_for(Engine, "connect")
        def _pragmas(dbapi, _):  # noqa: ANN001
            if dbapi.__class__.__module__.startswith("sqlite3"):
                cur = dbapi.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA busy_timeout=15000")
                cur.close()

    @login_manager.user_loader
    def _load(uid):
        u = db.session.get(User, int(uid))
        return u if u and u.is_active_flag else None

    @app.before_request
    def _locks_and_prefs():
        g.lock_held = False
        if request.method != "GET" and request.endpoint != "static" or request.path.startswith(("/ussd", "/sms")):
            WRITE_LOCK.acquire()
            g.lock_held = True
        g.theme = request.cookies.get("cwas_visit_theme", "saina")  # every new visit opens in Default
        if g.theme not in THEMES:
            g.theme = "saina"
        if current_user.is_authenticated:
            g.lang = current_user.language
            if current_user.must_change_password and request.endpoint not in ("change_password", "logout", "static", "set_language", "healthz", "offline", "service_worker", "manifest"):
                return redirect(url_for("change_password"))
        else:
            g.lang = request.cookies.get("cwas_lang") or negotiate_language(request.headers.get("Accept-Language", ""))
        if request.args.get("lang") in LANGS:
            g.lang = request.args["lang"]  # a shareable, crawlable address per language: ?lang=mg, ?lang=fr
        if request.headers.get("X-CWAS-Lang") in LANGS:
            g.lang = request.headers["X-CWAS-Lang"]  # the page fetched quietly in another language, so switching is instant
        if g.lang not in LANGS:
            g.lang = "en"

    @app.teardown_request
    def _unlock(exc):  # noqa: ANN001
        if getattr(g, "lock_held", False):
            g.lock_held = False
            try:
                if exc:
                    db.session.rollback()
            finally:
                WRITE_LOCK.release()

    @app.after_request
    def _headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # powerful browser features are off; the microphone stays available to this site only (assistant voice input)
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(self), geolocation=(), payment=(), usb=(), serial=()"
        resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        resp.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        resp.headers["X-Permitted-Cross-Domain-Policies"] = "none"
        resp.headers["Origin-Agent-Cluster"] = "?1"
        resp.headers.setdefault("Content-Security-Policy", CSP)
        if request.endpoint == "static":
            # pages link static files as ?v=<content hash> (see _static_v below), so such a copy never changes and the browser
            # keeps it for a year, loading it from its own cache; a file asked for without a version is rechecked daily
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable" if request.args.get("v") else "public, max-age=86400"
            _compress(resp)
        elif "cwas_theme" in request.cookies:  # the old year-long theme cookie; a theme now lasts for one visit
            resp.delete_cookie("cwas_theme", path="/")
        if IS_PROD or request.is_secure:
            resp.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        if request.endpoint != "static":
            try:
                signed_in = current_user.is_authenticated
            except Exception:  # noqa: BLE001  (the database may be the reason this response is an error page)
                signed_in = False
            # balances, receipts, exports and every signed-in page are never kept by the browser or a shared proxy
            if request.path.startswith(PRIVATE_PREFIXES) or signed_in or (request.method == "GET" and "text/html" in resp.content_type):
                resp.headers["Cache-Control"] = "no-store"
                resp.headers["Pragma"] = "no-cache"
            if not indexable() and request.endpoint not in ("robots_txt", "sitemap_xml", "security_txt"):
                resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        resp.headers.pop("Server", None)
        return resp

    asset_v = _asset_version(app.static_folder)
    # every static URL a template builds carries ?v=<hash of that file>. Fonts and anything the stylesheet names by URL stay
    # unversioned, so the page and the stylesheet always ask for the same address (one download, never two)
    css_file = Path(app.static_folder) / "css" / "app.css"
    css_refs = set(re.findall(r"url\(['\"]?/static/([^'\")?#]+)", css_file.read_text(encoding="utf-8"))) if css_file.exists() else set()
    file_v = {}

    @app.url_defaults
    def _static_v(endpoint, values):
        name = values.get("filename") or ""
        if endpoint != "static" or "v" in values or name.startswith("fonts/") or name in css_refs:
            return
        if name not in file_v:
            p = Path(app.static_folder) / name
            file_v[name] = hashlib.sha1(p.read_bytes()).hexdigest()[:10] if p.is_file() else ""
        if file_v[name]:
            values["v"] = file_v[name]
    app.jinja_env.filters["local"] = lambda dt: dt + timedelta(hours=3)  # stored in UTC, shown in Madagascar time
    app.jinja_env.globals["code128"] = barcode128.svg  # receipts: a scannable Code 128 of the booking reference
    app.jinja_env.globals["now_local"] = lambda: datetime.utcnow() + timedelta(hours=3)  # printed time on receipts

    # ── search engines: one address per language, canonical links and link previews ─────────────────────────────
    def site_base():
        """Public address of the site: SITE_URL when set (recommended in production), else this request's address."""
        return (os.environ.get("SITE_URL") or request.url_root).rstrip("/")

    def indexable():
        if request.endpoint == "simulator" and not app.config["SIMULATOR_PUBLIC"]:
            return False
        return request.endpoint in INDEXABLE and request.method == "GET"

    def lang_address(base, path, code):
        """English lives at the plain address; Malagasy and French at ?lang=mg and ?lang=fr."""
        return base + path + ("" if code == "en" else "?lang=" + code)

    def seo_meta(lang):
        base = site_base()
        return {"base": base, "canonical": lang_address(base, request.path, lang), "index": indexable(),
                "alternates": {c: lang_address(base, request.path, c) for c in LANGS}, "locale": OG_LOCALE.get(lang, "en_GB"),
                "image": base + url_for("static", filename="img/og/cwas-share.jpg")}

    @app.context_processor
    def _ctx():
        lang = g.get("lang", "en")
        return {"t": lambda s, **kw: tt(s, lang, **kw), "lang": lang, "seo": seo_meta(lang), "LANGS": LANGS, "THEMES": THEMES, "theme": g.get("theme", "saina"),
                "home_url": (url_for({"member": "dashboard", "coordinator": "coord_home", "admin": "admin_home"}[current_user.role]) if current_user.is_authenticated else url_for("login")), "fmt_ar": S.fmt_ar, "fmt_min": S.fmt_min, "USSD_CODE": S.DIAL, "SMS_CODE": os.environ.get("AT_SHORTCODE") or "7380",
                "asset_v": asset_v, "phones_script": U.demo_script, "VULN": S.VULN, "DISTANCE": S.DISTANCE, "distance_label": S.distance_label, "reported_level": S.reported_level, "last_note_id": _last_note, "unread": _unread_count, "render_notification": S.render_notification}

    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(429)
    @app.errorhandler(500)
    def _error(e):  # noqa: ANN001
        code = getattr(e, "code", 500)
        if request.path.startswith(("/api", "/ussd", "/sms", "/simulator/api")):
            return jsonify(error=code), code
        return render_template("error.html", code=code), code

    @app.errorhandler(CSRFError)
    def _csrf(e):  # noqa: ANN001
        if request.path.startswith(("/api", "/simulator/api")):
            return jsonify(error="csrf"), 400
        return render_template("error.html", code=400, csrf=True), 400

    @app.get("/healthz")
    @limiter.exempt
    def healthz():
        try:
            db.session.execute(text("SELECT 1"))
            return jsonify(status="ok", database=app.config["DB_KIND"]), 200
        except Exception:  # noqa: BLE001
            return jsonify(status="degraded", database="unreachable"), 503

    # ── robots.txt, sitemap.xml and security.txt ────────────────────────────────────────────────────────────
    @app.get("/robots.txt")
    @limiter.exempt
    def robots_txt():
        lines = ["User-agent: *", "Allow: /"] + [f"Disallow: {p}" for p in PRIVATE_PREFIXES] + ["", f"Sitemap: {site_base()}/sitemap.xml", ""]
        return "\n".join(lines), 200, {"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "public, max-age=3600"}

    @app.get("/sitemap.xml")
    @limiter.exempt
    def sitemap_xml():
        """Every public page in all three languages, each listing its alternates (hreflang) and x-default."""
        base = site_base()
        pages = [("index", {}, "1.0", "weekly"), ("water_points", {}, "0.9", "daily"), ("platform", {}, "0.8", "monthly"),
                 ("access", {}, "0.8", "monthly"), ("about", {}, "0.6", "monthly"), ("register", {}, "0.5", "monthly"),
                 ("legal", {"doc": "terms"}, "0.3", "yearly"), ("legal", {"doc": "privacy"}, "0.3", "yearly"), ("legal", {"doc": "refunds"}, "0.3", "yearly")]
        if app.config["SIMULATOR_PUBLIC"]:
            pages.append(("simulator", {}, "0.5", "monthly"))
        out = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">']
        for endpoint, args, priority, freq in pages:
            path = url_for(endpoint, **args)
            addr = {c: lang_address(base, path, c) for c in LANGS}
            links = "".join(f'<xhtml:link rel="alternate" hreflang="{c}" href="{escape(u)}"/>' for c, u in addr.items())
            links += f'<xhtml:link rel="alternate" hreflang="x-default" href="{escape(addr["en"])}"/>'
            out += [f"<url><loc>{escape(u)}</loc>{links}<changefreq>{freq}</changefreq><priority>{priority}</priority></url>" for u in addr.values()]
        out.append("</urlset>")
        return "\n".join(out), 200, {"Content-Type": "application/xml; charset=utf-8", "Cache-Control": "public, max-age=3600"}

    @app.get("/.well-known/security.txt")
    @limiter.exempt
    def security_txt():
        """RFC 9116: where to report a vulnerability. SECURITY_CONTACT (email or URL), else the administrator's email."""
        contact = os.environ.get("SECURITY_CONTACT") or os.environ.get("ADMIN_EMAIL") or "info@winebald.tech"
        if "@" in contact and not contact.startswith(("mailto:", "https://")):
            contact = "mailto:" + contact
        base, expires = site_base(), (datetime.utcnow() + timedelta(days=180)).strftime("%Y-%m-%dT00:00:00.000Z")
        body = (f"Contact: {contact}\nExpires: {expires}\nPreferred-Languages: en, fr\n"
                f"Canonical: {base}/.well-known/security.txt\nPolicy: {base}{url_for('legal', doc='privacy')}\n")
        return body, 200, {"Content-Type": "text/plain; charset=utf-8"}

    from web import register_routes
    register_routes(app)

    with app.app_context():
        db.create_all()
        _upgrade_schema()
        seed(app)
    if not app.config.get("TESTING") and not os.environ.get("CWAS_NO_SWEEPER"):
        threading.Thread(target=_sweeper, args=(app,), daemon=True, name="cwas-sweeper").start()
    return app


def _upgrade_schema():
    """Additive upgrades for a database made by an earlier release: create_all() adds missing tables but never changes
    existing ones. Runs at every start and does nothing once the database is current."""
    from models import Household, Notification
    insp = inspect(db.engine)
    users = {c["name"] for c in insp.get_columns(User.__tablename__)}
    homes = {c["name"] for c in insp.get_columns(Household.__tablename__)}
    false = "FALSE" if db.engine.dialect.name == "postgresql" else "0"
    with db.engine.begin() as conn:
        if "recovery_hash" not in users:
            conn.execute(text(f"ALTER TABLE {User.__tablename__} ADD COLUMN recovery_hash VARCHAR(255)"))
        for col, ddl in (("vuln_flags", "VARCHAR(80) NOT NULL DEFAULT ''"), ("needs_review", f"BOOLEAN NOT NULL DEFAULT {false}"),
                         ("home_source_id", "INTEGER"), ("home_source_note", "VARCHAR(80) NOT NULL DEFAULT ''")):
            if col not in homes:
                conn.execute(text(f"ALTER TABLE {Household.__tablename__} ADD COLUMN {col} {ddl}"))
        if db.engine.dialect.name == "postgresql":
            key = next((c for c in insp.get_columns(Notification.__tablename__) if c["name"] == "key"), None)
            if key is not None and getattr(key["type"], "length", None):
                # notification keys are whole sentences; the VARCHAR(64) of an earlier release rejects the longer ones
                conn.execute(text(f"ALTER TABLE {Notification.__tablename__} ALTER COLUMN key TYPE TEXT"))


def _sweeper(app):
    while True:
        time.sleep(60)
        try:
            with app.app_context(), WRITE_LOCK:
                S.sweep()
                db.session.remove()
        except Exception:  # noqa: BLE001
            log.exception("sweeper failed")


SAMPLE_SOURCES = [  # editable in the coordinator console; replace with the real Ampotaka water points
    ("Forage Ampotaka Centre", "borehole", "Ampotaka", -24.6915, 44.7212, 360, 1080, 30, 8, 120, 150),
    ("Puits Marché", "well", "Ampotaka", -24.6952, 44.7268, 360, 1020, 30, 6, 90, 120),
    ("Borne-fontaine École", "tap", "Ampotaka", -24.6887, 44.7183, 420, 1020, 20, 5, 70, 150),
    ("Forage Est", "borehole", "Ampotaka Est", -24.6931, 44.7340, 360, 1080, 30, 8, 120, 150),
    ("Puits Ouest", "well", "Ampotaka Ouest", -24.6976, 44.7101, 360, 1020, 30, 6, 90, 120),
]


def seed(app):
    """Idempotent. Always: the super admin and sample water points. Demo people only outside production."""
    if not User.query.filter_by(role="admin").first():
        email = os.environ.get("ADMIN_EMAIL", "info@winebald.tech").lower()
        pw = os.environ.get("ADMIN_PASSWORD", "Winebald @123")
        db.session.add(User(role="admin", name="Winebald", email=email, password_hash=generate_password_hash(pw), language="en",
                            must_change_password=True))
        db.session.flush()
        S.audit("seed.admin", "user", email, "super admin created; password change required", channel="system")
        if not os.environ.get("ADMIN_PASSWORD"):
            log.warning("Seeded super admin with the default password. It must be changed at first sign-in; "
                        "set ADMIN_PASSWORD before the first start to avoid a known default.")
    if not WaterSource.query.first():
        for n, k, v, la, lo, o, c, sm, sc, dc, tf in SAMPLE_SOURCES:
            db.session.add(WaterSource(name=n, kind=k, village=v, latitude=la, longitude=lo, open_min=o, close_min=c, slot_minutes=sm,
                                       slot_capacity=sc, daily_capacity=dc, tariff_per_100l=tf))
    if IS_PROD:
        for key, label in (("enroll_coord", "COORD"), ("enroll_admin", "ADMIN")):
            if not db.session.get(Setting, key):
                S.set_setting(key, f"AMP-{label}-" + secrets.token_hex(4).upper())
    db.session.commit()
    if os.environ.get("SEED_DEMO", "0" if IS_PROD else "1") == "1" and not User.query.filter_by(role="coordinator").first():
        from seed_demo import seed_demo
        seed_demo()


app = create_app()

if __name__ == "__main__":
    from waitress import serve
    port = int(os.environ.get("PORT", "5000") or 5000)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(f"CWAS on http://0.0.0.0:{port}  (database: {app.config['DB_KIND']})", flush=True)
    serve(app, host="0.0.0.0", port=port, threads=8, ident="cwas")
