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

    # ── coordinator ──
    def test_20_coordinator_console(self):
        c = login("coordinator@cwas.demo", DEMO_PW)
        for p in ("/coord", "/coord/queue", "/coord/bookings", "/coord/sources", "/coord/sources/new", "/coord/maintenance", "/coord/households", "/coord/deposits",
                  "/coord/reports", "/coord/insights", "/coord/announce", "/coord/export/bookings.csv", "/coord/export/wallet.csv", "/coord/export/equity.csv", "/coord/export/households.csv"):
            self.assertEqual(c.get(p).status_code, 200, p)
        self.assertEqual(c.get("/admin").status_code, 403)
        with self.app.app_context():
            pend = Booking.query.filter_by(status="pending").first()
            hid = pend.household_id
            bid = pend.id
        self.assertEqual(c.get(f"/coord/households/{hid}").status_code, 200)
        r = post(c, f"/coord/queue/{bid}", {"action": "approve", "note": "ok"})
        self.assertEqual(r.status_code, 302)
        with self.app.app_context():
            self.assertEqual(db.session.get(Booking, bid).status, "approved")
        post(c, f"/coord/bookings/{bid}/collected")
        with self.app.app_context():
            self.assertEqual(db.session.get(Booking, bid).status, "collected")
        r = post(c, f"/coord/households/{hid}", {"section": "cash", "amount": "3000"})
        self.assertEqual(r.status_code, 302)
        post(c, "/coord/announce", {"message": "Water point closed tomorrow morning."})
        with self.app.app_context():
            self.assertTrue(S.reconcile_wallet(db.session.get(Household, hid)))

    def test_21_source_crud_and_maintenance_refund(self):
        c = login("coordinator@cwas.demo", DEMO_PW)
        r = post(c, "/coord/sources/new", {"name": "Test Puits", "kind": "well", "village": "Ampotaka", "status": "operational", "open": "06:00", "close": "18:00",
                                          "slot_minutes": "30", "slot_capacity": "4", "daily_capacity": "40", "tariff": "100", "max_litres": "100"})
        self.assertEqual(r.status_code, 302)
        with self.app.app_context():
            from models import WaterSource
            src = WaterSource.query.filter_by(name="Test Puits").one()
            h = User.query.filter_by(phone="+261340000104").first().household
            day = next(d for d in S.booking_days() if S.slot_list(src, d, only_open=True) and not Booking.query.filter_by(household_id=h.id, date=d).filter(Booking.status.in_(Booking.ACTIVE)).first())
            slot = S.slot_list(src, day, only_open=True)[-1]
            b = S.create_booking(h, src.id, day, slot["start_min"], 20, "web")
            db.session.commit()
            before, sid, bid = h.balance, src.id, b.id
        from datetime import datetime, timedelta
        start = datetime.combine(day, datetime.min.time())
        r = post(c, "/coord/maintenance", {"source": sid, "start": start.strftime("%Y-%m-%dT%H:%M"), "end": (start + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"), "reason": "Pump repair"})
        self.assertEqual(r.status_code, 302)
        with self.app.app_context():
            self.assertEqual(db.session.get(Booking, bid).status, "cancelled")
            self.assertEqual(db.session.get(Household, h.id).balance, before + db.session.get(Booking, bid).amount)
            self.assertEqual(S.slot_list(db.session.get(WaterSource, sid), day)[-1]["state"], "blocked")
        r = post(c, f"/coord/sources/{sid}/delete")
        self.assertEqual(r.status_code, 302)

    # ── admin ──
    def test_30_admin_console(self):
        with self.app.app_context():
            u = User.query.filter_by(role="admin").first()
            u.mfa_enabled, u.mfa_secret, u.must_change_password = False, None, False
            db.session.commit()
        c = login("info@winebald.tech", "Pilot Water #2026x")
        for p in ("/admin", "/admin/users", "/admin/settings", "/admin/audit", "/admin/audit?verify=1", "/admin/audit.csv", "/admin/database", "/admin/channels"):
            self.assertEqual(c.get(p).status_code, 200, p)
        self.assertIn("intact", c.get("/admin/audit?verify=1").get_data(as_text=True))
        r = post(c, "/admin/users/create", {"name": "Test Member", "email": "tm@example.com", "phone": "+261340009999", "role": "member", "password": "Temp Pass #2026a", "village": "Ampotaka"})
        self.assertEqual(r.status_code, 302)
        post(c, "/admin/settings", {"discount_elevated": "10", "discount_high": "25", "min_deposit": "500", "max_deposit": "200000", "no_show_grace_min": "15", "auto_approve": "1", "cash_enabled": "1"})
        post(c, "/admin/database", {"action": "backup"})
        files = os.listdir(os.path.join(_TMP, "backups"))
        self.assertTrue(files)
        with self.app.app_context():
            n_before = Booking.query.count()
            b_before = sum(h.balance for h in Household.query.all())
        post(c, "/admin/database", {"action": "restore", "file": files[0], "confirm": "RESTORE"})
        with self.app.app_context():
            self.assertEqual(Booking.query.count(), n_before)
            self.assertEqual(sum(h.balance for h in Household.query.all()), b_before)
            ok, bad, n = S.verify_audit_chain()
            self.assertTrue(ok, bad)

    def test_31_audit_tamper_detected(self):
        with self.app.app_context():
            from models import AuditLog
            row = AuditLog.query.order_by(AuditLog.id).offset(3).first()
            old = row.detail
            row.detail = "tampered"
            db.session.commit()
            self.assertFalse(S.verify_audit_chain()[0])
            row.detail = old
            db.session.commit()
            self.assertTrue(S.verify_audit_chain()[0])


    # ── USSD and SMS ──
    _n = [0]

    def ussd(self, phone, text, sid="S1", path="/ussd"):
        c = A.app.test_client()
        return c.post(path, data={"sessionId": sid, "phoneNumber": phone, "text": text, "serviceCode": "*384*9411#", "networkCode": "99999"}).get_data(as_text=True)

    def play(self, phone, *steps, sid=None):
        """Plays a whole USSD session hop by hop, exactly as the telco does, and returns the last screen."""
        Platform._n[0] += 1
        sid = sid or f"R{Platform._n[0]}"
        out = ""
        for i in range(len(steps) + 1):
            out = self.ussd(phone, "*".join(steps[:i]), sid=sid, path=("/ussd", "/api/ussd", "/webhooks/ussd")[i % 3])
            self.assertLessEqual(len(out) - 4, 182, out)
        return out

    def sms_to(self, phone):
        with self.app.app_context():
            return [m.body for m in SmsLog.query.filter_by(direction="out", phone=phone).order_by(SmsLog.id)]
