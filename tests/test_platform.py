"""End-to-end tests: python3 -m unittest discover -s tests -v
Each run uses a throwaway database, so nothing in instance/ is touched."""
import os
import re
import shutil
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="cwas-test-")
os.environ.update(CWAS_INSTANCE=_TMP, CWAS_NO_SWEEPER="1", CWAS_NO_LIMITS="1", SEED_DEMO="1", ADMIN_EMAIL="info@winebald.tech", ADMIN_PASSWORD="Winebald @123")
os.environ.pop("DATABASE_URL", None)

import app as A  # noqa: E402
import services as S  # noqa: E402
from i18n import MISSING  # noqa: E402
from models import Booking, Household, SmsLog, User, WalletTxn, WaterSource, db  # noqa: E402
from werkzeug.security import check_password_hash, generate_password_hash  # noqa: E402

DEMO_PW = "Demo Water @2026"


def token(c):
    html = c.get("/offline").get_data(as_text=True)
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def post(c, url, data=None, **kw):
    d = dict(data or {})
    d["csrf_token"] = token(c)
    return c.post(url, data=d, **kw)


def login(ident, pw):
    c = A.app.test_client()
    r = post(c, "/login", {"identifier": ident, "password": pw})
    assert r.status_code == 302, r.get_data(as_text=True)[:400]
    return c


class Platform(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = A.app

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_TMP, ignore_errors=True)

    # ── security basics ──
    def test_01_csrf_and_headers(self):
        c = A.app.test_client()
        self.assertEqual(c.post("/login", data={"identifier": "x", "password": "y"}).status_code, 400)
        r = c.get("/")
        self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(c.get("/healthz").get_json()["status"], "ok")

    def test_02_admin_forced_password_change_and_mfa(self):
        c = login("info@winebald.tech", "Winebald @123")
        r = c.get("/admin")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/account/password", r.headers["Location"])
        r = post(c, "/account/password", {"current": "Winebald @123", "password": "Winebald @123"})
        self.assertEqual(r.status_code, 200)  # same password refused
        r = post(c, "/account/password", {"current": "Winebald @123", "password": "Pilot Water #2026x"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(c.get("/admin").status_code, 200)
        post(c, "/account/security", {"action": "start"})
        html = c.get("/account/security").get_data(as_text=True)
        secret = re.search(r'break-all font-mono text-xs">([A-Z2-7]+)<', html).group(1)
        self.assertIn("<svg", html)
        code = S._hotp(secret, int(__import__("time").time() // 30))
        post(c, "/account/security", {"action": "enable", "code": f"{code:06d}" if isinstance(code, int) else code})
        with self.app.app_context():
            self.assertTrue(User.query.filter_by(role="admin").first().mfa_enabled)
        c2 = A.app.test_client()
        r = post(c2, "/login", {"identifier": "info@winebald.tech", "password": "Pilot Water #2026x"})
        self.assertIn("/login/mfa", r.headers["Location"])

    def test_03_lockout(self):
        c = A.app.test_client()
        for _ in range(5):
            post(c, "/login", {"identifier": "+261340000108", "password": "wrong"})
        r = post(c, "/login", {"identifier": "+261340000108", "password": DEMO_PW})
        self.assertEqual(r.status_code, 200)  # locked even with the right password
        with self.app.app_context():
            u = User.query.filter_by(phone="+261340000108").first()
            u.locked_until, u.failed_logins = None, 0
            db.session.commit()
