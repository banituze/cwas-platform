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

    # ── member journey ──
    def test_10_member_book_pay_cancel(self):
        c = login("+261340000106", DEMO_PW)
        for p in ("/app", "/app/book", "/app/bookings", "/app/wallet", "/app/water-points", "/app/notifications", "/app/profile", "/app/assistant"):
            self.assertEqual(c.get(p).status_code, 200, p)
        with self.app.app_context():
            h = User.query.filter_by(phone="+261340000106").first().household
            src = S.operational_sources()[0]
            day = next(d for d in reversed(S.booking_days()) if not Booking.query.filter_by(household_id=h.id, date=d).filter(Booking.status.in_(Booking.ACTIVE)).first())
            slot = S.slot_list(src, day, only_open=True)[3]
            bal0 = h.balance
        r = post(c, "/app/book", {"source": src.id, "date": day.isoformat(), "slot": slot["start_min"], "litres": 40})
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:300])
        ref = r.headers["Location"].rsplit("/", 1)[1]
        self.assertEqual(c.get(f"/app/bookings/{ref}").status_code, 200)
        self.assertEqual(c.get(f"/app/bookings/{ref}/receipt").status_code, 200)
        r = post(c, "/app/book", {"source": src.id, "date": day.isoformat(), "slot": slot["start_min"], "litres": 40})
        self.assertEqual(r.status_code, 200)  # second booking the same day is refused
        with self.app.app_context():
            h = db.session.get(Household, h.id)
            self.assertLess(h.balance, bal0)
            self.assertTrue(S.reconcile_wallet(h))
        post(c, f"/app/bookings/{ref}/cancel")
        with self.app.app_context():
            h = db.session.get(Household, h.id)
            self.assertEqual(h.balance, bal0)
            self.assertTrue(S.reconcile_wallet(h))
            self.assertEqual(WalletTxn.query.filter_by(household_id=h.id, kind="booking_refund").filter(WalletTxn.booking.has(ref=ref)).count(), 1)
        post(c, f"/app/bookings/{ref}/cancel")  # a second cancel must not refund twice
        with self.app.app_context():
            self.assertEqual(db.session.get(Household, h.id).balance, bal0)

    def test_11_member_cannot_reach_staff_or_others(self):
        c = login("+261340000107", DEMO_PW)
        for p in ("/coord", "/admin", "/coord/queue", "/admin/users"):
            self.assertEqual(c.get(p).status_code, 403, p)
        with self.app.app_context():
            other = Booking.query.join(Household).join(User).filter(User.phone != "+261340000107").first().ref
        self.assertEqual(c.get(f"/app/bookings/{other}").status_code, 404)

    def test_12_wallet_deposit(self):
        c = login("+261340000105", DEMO_PW)
        r = post(c, "/app/wallet", {"provider": "orange", "amount": "2000"})
        self.assertEqual(r.status_code, 302)
        r = post(c, "/app/wallet", {"provider": "orange", "amount": "5"})
        self.assertEqual(r.status_code, 200)
        with self.app.app_context():
            self.assertTrue(S.reconcile_wallet(User.query.filter_by(phone="+261340000105").first().household))
            self.assertEqual(S.fmt_ar(1500), "1,500 MGA")
