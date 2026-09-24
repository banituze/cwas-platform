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

    def test_40_ussd_language_then_menu_pin_only_at_confirmation(self):
        out = self.ussd("+261340000102", "")
        self.assertTrue(out.startswith("CON"))
        self.assertEqual(out, "CON Tongasoa eto amin'ny CWAS/Welcome to CWAS/Bienvenue sur CWAS\n\n"
                              "Safidio ny fiteny/Choose language/Choisissez votre langue:\n\n1. Malagasy\n2. English\n3. Francais\n\n99. Exit")
        menu = self.play("+261340000102", "2")
        for item in ("1. Deposit funds", "2. Book water", "3. My bookings", "4. Cancel booking", "5. Balance", "6. Notifications",
                     "7. Water points", "8. My profile", "9. Help", "10. Receipts", "99. Exit"):
            self.assertIn(item, menu)
        self.assertNotIn("98. More", menu)  # room on the screen means no paging
        self.assertNotIn("PIN", menu)       # no PIN just to look around
        with self.app.app_context():
            first = User.query.filter_by(phone="+261340000102").first().name.split()[0]
        self.assertIn(f"Hello {first}\n", menu)  # first name only, never cut mid-word
        self.assertIn("MGA", self.play("+261340000102", "2", "5"))
        self.assertIn("Thank you for using CWAS.", self.play("+261340000102", "2", "99"))
        self.assertIn("1. Orange Money", self.play("+261340000102", "2", "1"))
        self.assertIn("1. Deposit funds", self.play("+261340000102", "2", "1", "97"))
        self.assertIn("1. Deposit funds", self.play("+261340000102", "2", "1", "1", "00"))
        pin = self.play("+261340000102", "2", "1", "1", "1", "1")
        self.assertIn("Enter your 4-digit PIN to confirm", pin)
        self.assertIn("0. Forgot PIN", pin)

    def test_41_ussd_wrong_pin_ends_session_and_locks_after_three(self):
        phone = "+261340000103"
        first = self.play(phone, "2", "1", "1", "1", "1", "0000", sid="L0")
        self.assertTrue(first.startswith("END PIN incorrect."), first)
        self.assertIn("Attempts left: 2", first)
        for i in (1, 2):
            last = self.play(phone, "2", "1", "1", "1", "1", "0000", sid=f"L{i}")
        self.assertIn("Too many wrong PINs", last)
        self.assertIn("Too many wrong PINs", self.play(phone, "2", "1", "1", "1", "1", sid="Lx"))
        self.assertIn("MGA", self.play(phone, "2", "5", sid="Ly"))  # reading still works while the PIN is locked
        with self.app.app_context():
            u = User.query.filter_by(phone=phone).first()
            u.pin_failed, u.pin_locked_until = 0, None
            db.session.commit()

    def test_42_full_member_journey_by_ussd_crucial_sms_only(self):
        with self.app.app_context():
            S.set_setting("auto_approve", "0")
            db.session.commit()
        phone = "+261340007001"
        self.assertIn("Register as:", self.play(phone, "2"))
        self.assertIn("recovery code", self.play(phone, "2", "1", "Winebald Banituze", "Ampotaka", "5", "0", "2", "1", "1234", "1234"))
        out = self.play(phone, "2", "1", "Winebald Banituze", "Ampotaka", "5", "0", "2", "1", "1234", "1234", "246810", "246810")
        self.assertTrue(out.startswith("END Welcome Winebald!"), out)
        self.assertIn("Dial *384*9411# to book a slot.", out)
        out = self.play(phone, "2", "1", "1", "3", "1", "1234")
        self.assertIn("Deposit recorded", out)
        self.assertIn("+5,000 MGA", out)
        self.assertRegex(out, r"Ref: WT-[0-9A-F]{8}")
        self.assertIn("Balance: 5,000 MGA", out)
        book = self.play(phone, "2", "2", "1", "2", "1", "1", "1", "1234")
        self.assertTrue(book.startswith("END Booked!"), book)
        ref = re.search(r"Ref: (CW-[0-9A-F]{8})", book).group(1)
        self.assertIn("Status: pending approval", book)
        self.assertIn(ref, self.play(phone, "2", "3"))
        self.assertIn("Confirm booking", self.play(phone, "2", "2", "1", "2", "1", "1"))
        cancelled = self.play(phone, "2", "4", "1", "1", "1234")
        self.assertIn(f"Cancelled {ref}. Eligible payment was refunded once.", cancelled)
        self.assertIn("Balance: 5,000 MGA", self.play(phone, "2", "5") + cancelled)
        self.assertTrue(self.play(phone, "2", "4").startswith("END"))  # nothing left to cancel
        with self.app.app_context():
            self.assertTrue(S.reconcile_wallet(User.query.filter_by(phone=phone).first().household))
        notes = self.play(phone, "2", "6")
        for title in ("Booking cancelled", "Deposit recorded", "Booking created"):
            self.assertIn(title, notes)
        self.assertIn("[OPER]", self.play(phone, "2", "7"))
        self.assertIn("Receipts", self.play(phone, "2", "10"))
        self.assertIn("Language set to EN", self.play(phone, "2", "8", "5", "2"))
        bodies = " || ".join(self.sms_to(phone))
        for expect in ("Welcome Winebald!", "Deposit WT-", "recorded: +5,000 MGA via Orange Money"):
            self.assertIn(expect, bodies)
        for quiet in ("Booked CW-", f"Booking {ref} was cancelled", "Language set"):  # routine news stays in the app
            self.assertNotIn(quiet, bodies)

    def test_43_replay_never_posts_twice(self):
        phone = "+261340000104"
        with self.app.app_context():
            before = User.query.filter_by(phone=phone).first().household.balance
        a = self.play(phone, "2", "1", "1", "1", "1", "1234", sid="ONCE")
        b = self.ussd(phone, "2*1*1*1*1*1234", sid="ONCE")
        self.assertEqual(a, b)
        self.assertIn("Deposit recorded", a)
        with self.app.app_context():
            self.assertEqual(db.session.get(Household, User.query.filter_by(phone=phone).first().household.id).balance, before + 1000)

    def test_44_coordinator_registration_needs_the_enrollment_code(self):
        phone = "+261340007002"
        self.assertIn("Code not recognised", self.play(phone, "2", "2", "WRONG-CODE"))
        out = self.play(phone, "2", "2", "AMPOTAKA-COORD", "Naly Test", "4321", "4321", "135790", "135790")
        self.assertTrue(out.startswith("END Welcome Naly!"), out)
        with self.app.app_context():
            self.assertEqual(User.query.filter_by(phone=phone).first().role, "coordinator")
        for i in range(3):
            self.play("+261340007003", "2", "2", "NOPE" + str(i), sid=f"E{i}")
        self.assertIn("Too many attempts", self.play("+261340007003", "2", "2", sid="E9"))
        with self.app.app_context():
            self.assertIsNone(User.query.filter_by(phone="+261340007003").first())

    def test_45_coordinator_menu_and_approval_by_ussd(self):
        menu = self.play("+261340000001", "2")
        for item in ("1. Pending queue", "2. Approve", "3. Deny", "4. Mark collected", "5. Water points", "6. Register household", "7. Operations summary"):
            self.assertIn(item, menu)
        self.assertIn("Today", self.play("+261340000001", "2", "7"))
        with self.app.app_context():
            before = Booking.query.filter_by(status="pending").count()
        self.assertGreater(before, 0)
        self.assertIn("Pending (", self.play("+261340000001", "2", "1"))
        self.assertIn("1. Approve", self.play("+261340000001", "2", "1", "1"))
        self.assertIn("Enter your 4-digit PIN", self.play("+261340000001", "2", "1", "1", "1"))
        done = self.play("+261340000001", "2", "1", "1", "1", "2468")
        self.assertRegex(done, r"^END Approved CW-[0-9A-F]{8}")
        with self.app.app_context():
            self.assertEqual(Booking.query.filter_by(status="pending").count(), before - 1)
        reg = self.play("+261340000001", "2", "6", "Ussd House", "0340007555", "Ampotaka", "6", "13", "3", "1", "2", "4321", "1", "2468")
        self.assertIn("Registered Ussd House", reg)
        with self.app.app_context():
            self.assertIsNotNone(User.query.filter_by(phone="+261340007555").first())
        self.assertTrue(any("Welcome Ussd!" in b for b in self.sms_to("+261340007555")))

    def test_46_sms_member_commands_and_confirmations(self):
        with self.app.app_context():
            S.set_setting("auto_approve", "0")
            db.session.commit()
        phone = "+261340000108"
        c = A.app.test_client()

        def sms(text, who=phone):
            return c.post("/api/sms/inbound", data={"from": who, "to": "7380", "text": text}).status_code

        for t in ("HELP", "BAL", "SOURCES", "BOOKINGS", "NOTICES", "PROFILE"):
            self.assertEqual(sms(t), 200)
        self.assertEqual(sms("DEPOSIT 3000"), 200)
        self.assertEqual(sms("BOOK 2 TOMORROW 08:00 20"), 200)
        bodies = self.sms_to(phone)
        joined = " || ".join(bodies)
        for expect in ("CWAS SMS: BAL", "Balance:", "Forage", "recorded: +3,000 MGA", "Booked CW-"):
            self.assertIn(expect, joined)
        ref = re.search(r"Booked (CW-[0-9A-F]{8})", joined).group(1)
        self.assertEqual(sms(f"BOOKING {ref}"), 200)
        self.assertEqual(sms(f"CANCEL {ref}"), 200)
        self.assertIn(f"Booking {ref} was cancelled", " || ".join(self.sms_to(phone)))
        self.assertEqual(sms("LANG FR"), 200)
        self.assertTrue(any("Langue définie sur FR" in b for b in self.sms_to(phone)))
        sms("LANG EN")
        with self.app.app_context():
            self.assertTrue(S.reconcile_wallet(User.query.filter_by(phone=phone).first().household))

    def test_47_sms_staff_commands_and_no_privilege_from_words(self):
        with self.app.app_context():
            b = Booking.query.filter_by(status="pending").first()
            ref, member_phone = b.ref, b.household.user.phone
        c = A.app.test_client()
        member = "+261340000106"
        c.post("/sms/incoming", data={"from": member, "text": f"APPROVE {ref}"})
        c.post("/sms/incoming", data={"from": member, "text": "REG MEMBER X|+261340009111|V|3|en|1234"})
        with self.app.app_context():
            self.assertEqual(Booking.query.filter_by(ref=ref).first().status, "pending")
            self.assertIsNone(User.query.filter_by(phone="+261340009111").first())
        staff = "+261340000001"
        n0 = len(self.sms_to(member_phone))
        c.post("/sms/incoming", data={"from": staff, "text": "PENDING"})
        self.assertTrue(any(ref in m for m in self.sms_to(staff)))
        c.post("/sms/incoming", data={"from": staff, "text": f"APPROVE {ref}"})
        with self.app.app_context():
            self.assertEqual(Booking.query.filter_by(ref=ref).first().status, "approved")
        after = self.sms_to(member_phone)
        self.assertGreater(len(after), n0)
        self.assertIn(ref, after[-1])
        c.post("/sms/incoming", data={"from": staff, "text": "REG MEMBER Test Sms|+261340007888|Ampotaka|3|en|4321"})
        with self.app.app_context():
            self.assertIsNotNone(User.query.filter_by(phone="+261340007888").first())
        self.assertTrue(any("Welcome Test!" in m for m in self.sms_to("+261340007888")))
        c.post("/api/sms/inbound", data={"from": "+261340007999", "text": "REGISTER MEMBER Sms Person|Ampotaka|4|fr|4321"})
        with self.app.app_context():
            u = User.query.filter_by(phone="+261340007999").first()
            self.assertEqual((u.role, u.language), ("member", "fr"))
        self.assertEqual(c.post("/api/sms/delivery", data={"id": "1", "status": "Success"}).status_code, 200)

    def test_48_telco_token_guard_and_aliases(self):
        os.environ["AT_WEBHOOK_TOKEN"] = "s3cret"
        try:
            c = A.app.test_client()
            for path in ("/ussd", "/api/ussd", "/webhooks/ussd"):
                self.assertEqual(c.post(path, data={"sessionId": "T", "phoneNumber": "+261340000101", "text": ""}).status_code, 403)
                self.assertEqual(c.post(path + "?token=s3cret", data={"sessionId": "T", "phoneNumber": "+261340000101", "text": ""}).status_code, 200)
            self.assertEqual(c.post("/api/sms/inbound", data={"from": "+261340000101", "text": "HELP"}).status_code, 403)
        finally:
            del os.environ["AT_WEBHOOK_TOKEN"]

    def test_49_payment_webhook_signature_and_live_mode(self):
        import hashlib
        import hmac
        import json
        c = A.app.test_client()
        self.assertEqual(c.post("/webhooks/payments/orange", data="{}").status_code, 503)
        os.environ["PAYMENT_WEBHOOK_SECRET"] = "k"
        try:
            with self.app.app_context():
                h = User.query.filter_by(phone="+261340000101").first().household
                os.environ["PAYMENT_MODE"] = "live"
                txn = S.deposit(h, 1000, "orange", None, "web")
                db.session.commit()
                ref, bal = txn.reference, h.balance
                self.assertEqual(txn.status, "pending")
            body = json.dumps({"reference": ref, "status": "success"}).encode()
            self.assertEqual(c.post("/webhooks/payments/orange", data=body, headers={"X-Signature": "bad"}).status_code, 403)
            sig = hmac.new(b"k", body, hashlib.sha256).hexdigest()
            self.assertEqual(c.post("/webhooks/payments/orange", data=body, headers={"X-Signature": sig}).status_code, 200)
            self.assertEqual(c.post("/webhooks/payments/orange", data=body, headers={"X-Signature": sig}).status_code, 404)
            with self.app.app_context():
                self.assertEqual(db.session.get(Household, h.id).balance, bal + 1000)
            self.assertIn(ref, self.sms_to("+261340000101")[-1])
        finally:
            os.environ.pop("PAYMENT_WEBHOOK_SECRET", None)
            os.environ.pop("PAYMENT_MODE", None)

    def test_50_production_defaults_to_live_payments(self):
        os.environ["CWAS_ENV"] = "production"
        try:
            self.assertEqual(S.payment_mode(), "live")
        finally:
            os.environ.pop("CWAS_ENV", None)
        self.assertEqual(S.payment_mode(), "simulation")

    def test_51_simulator_api(self):
        c = A.app.test_client()
        r = c.post("/simulator/api/ussd", json={"phone": "+261340000101", "session": "abc", "text": ""}, headers={"X-CSRFToken": token(c)})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Welcome to CWAS", r.get_json()["screen"])
        r = c.post("/simulator/api/sms", json={"phone": "+261340000101", "text": "BAL"}, headers={"X-CSRFToken": token(c)})
        self.assertIn("MGA", r.get_json()["reply"])
        self.assertEqual(c.get("/simulator/api/inbox?phone=%2B261340000101&after=0").status_code, 200)

    # ── PIN, recovery code, My profile, PIN help, crucial-only SMS ──
    def test_52_profile_recovery_code_and_forgot_pin_by_ussd(self):
        phone = "+261340007101"
        out = self.play(phone, "2", "1", "Hery Rabe", "Ampotaka", "4", "0", "2", "1", "1357", "1357", "112233", "112233")
        self.assertTrue(out.startswith("END Welcome Hery!"), out)
        view = self.play(phone, "2", "8", "1")
        for w in ("Hery Rabe", "034 00 071 01", "Ampotaka, 4 people", "Recovery code: saved"):
            self.assertIn(w, view)
        self.assertIn("Saved.", self.play(phone, "2", "8", "3", "Andoharano", "1", "1357"))
        with self.app.app_context():
            self.assertEqual(User.query.filter_by(phone=phone).first().household.village, "Andoharano")
        # Forgot PIN from the PIN prompt of a deposit: the recovery code sets a new PIN and the deposit still goes through
        out = self.play(phone, "2", "1", "1", "1", "1", "0", "1", "112233", "2468", "2468")
        self.assertTrue(out.startswith("END Deposit recorded"), out)
        self.assertTrue(self.play(phone, "2", "1", "1", "1", "1", "2468").startswith("END Deposit recorded"))
        self.assertIn("Recovery code incorrect.", self.play(phone, "2", "8", "8", "1", "999999"))
        self.assertIn("PIN changed.", self.play(phone, "2", "8", "6", "2468", "8642", "8642"))
        self.assertIn("Recovery code saved", self.play(phone, "2", "8", "7", "8642", "654321", "654321"))
        bodies = " || ".join(self.sms_to(phone))
        self.assertIn("Your PIN was changed", bodies)
        self.assertIn("Your recovery code was changed", bodies)
        self.assertIn("Request sent", self.play(phone, "2", "8", "8", "2", "1"))
        self.assertIn("already open", self.play(phone, "2", "8", "8", "2", "1"))  # once an hour
        with self.app.app_context():
            self.assertTrue(SmsLog.query.filter(SmsLog.body.like("PIN help:%Hery Rabe%")).first())
        self.assertIn("deleted", self.play(phone, "2", "8", "9", "1", "8642"))
        with self.app.app_context():
            self.assertIsNone(User.query.filter_by(phone=phone).first())

    def test_53_staff_pin_reset_sms_pin_help_and_deposit_rejection(self):
        member = "+261340000105"
        c = A.app.test_client()
        c.post("/api/sms/inbound", data={"from": member, "to": "7380", "text": "PIN HELP"})
        self.assertIn("Request sent", self.sms_to(member)[-1])
        c.post("/api/sms/inbound", data={"from": "+261349999998", "to": "7380", "text": "forgot pin"})
        self.assertIn("no account", self.sms_to("+261349999998")[-1])
        staff_menu = self.play("+261340000001", "2", "98")
        self.assertIn("12. Reset household PIN", staff_menu)
        self.assertIn("Check with the caller", self.play("+261340000001", "2", "98", "12", "0340000105"))
        out = self.play("+261340000001", "2", "98", "12", "0340000105", "1", "2468")
        self.assertIn("PIN reset for", out)
        temp = re.search(r"Temporary PIN: (\d{4})", self.sms_to(member)[-1]).group(1)
        self.assertTrue(self.play(member, "2", "1", "1", "1", "1", temp).startswith("END Deposit recorded"))
        with self.app.app_context():
            u = User.query.filter_by(phone=member).first()
            staff = User.query.filter_by(phone="+261340000001").first()
            cols = {col.name for col in WalletTxn.__table__.columns}
            kw = dict(household_id=u.household.id, kind="deposit", amount=2000, status="pending", reference="WT-REJECT01")
            for k, v in (("provider", "orange"), ("channel", "web")):
                if k in cols:
                    kw[k] = v
            txn = WalletTxn(**kw)
            db.session.add(txn)
            db.session.commit()
            S.reject_pending_deposit(txn, staff, "web")
            db.session.commit()
            self.assertEqual(txn.status, "failed")
            self.assertTrue(S.reconcile_wallet(u.household))
            u.pin_hash = generate_password_hash("1234")
            db.session.commit()
        self.assertIn("WT-REJECT01 of 2,000 MGA was not confirmed", self.sms_to(member)[-1])

    def test_54_sms_sender_id_is_sent_and_falls_back_when_not_registered(self):
        import json
        import urllib.parse
        import urllib.request
        calls = []

        class Resp:
            def __init__(self, body):
                self.body = body

            def read(self):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake(req, timeout=0):
            data = dict(urllib.parse.parse_qsl(req.data.decode()))
            calls.append(data)
            status = "InvalidSenderId" if len(calls) == 1 else "Success"
            return Resp(json.dumps({"SMSMessageData": {"Recipients": [{"status": status}]}}).encode())

        env = {"SMS_ENABLED": "1", "AT_API_KEY": "k", "AT_USERNAME": "sandbox", "AT_SENDER_ID": "CWAS"}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        real, urllib.request.urlopen = urllib.request.urlopen, fake
        try:
            with self.app.app_context():
                self.assertEqual(S.send_sms("+261340000101", "hello").status, "sent")
                db.session.rollback()
        finally:
            urllib.request.urlopen = real
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].get("from"), "CWAS")  # the sender goes out in the sandbox too
        self.assertNotIn("from", calls[1])               # retried without it when not registered

    def test_55_web_pin_recovery_code_forgot_pin_and_coordinator_reset(self):
        phone = "+261340000106"
        cc = login(phone, DEMO_PW)
        post(cc, "/app/profile", {"section": "pin", "current": DEMO_PW, "pin": "5555", "pin2": "5555"})
        post(cc, "/app/profile", {"section": "recovery", "current": DEMO_PW, "code": "121212", "code2": "121212"})
        with self.app.app_context():
            u = User.query.filter_by(phone=phone).first()
            self.assertTrue(check_password_hash(u.pin_hash, "5555"))
            self.assertTrue(u.recovery_hash)
        c = A.app.test_client()
        self.assertEqual(c.get("/forgot-pin").status_code, 200)
        post(c, "/forgot-pin", {"phone": "0340000106", "code": "000000", "pin": "1111", "pin2": "1111"})
        post(c, "/forgot-pin", {"phone": "0340000106", "code": "121212", "pin": "7777", "pin2": "7777"})
        with self.app.app_context():
            self.assertTrue(check_password_hash(User.query.filter_by(phone=phone).first().pin_hash, "7777"))
        coord = login("coordinator@cwas.demo", DEMO_PW)
        with self.app.app_context():
            hid = User.query.filter_by(phone="+261340000107").first().household.id
        rule = next(r for r in A.app.url_map.iter_rules() if r.endpoint == "coord_household")
        arg = next(iter(rule.arguments))
        url = re.sub(r"<(?:\w+:)?" + arg + ">", str(hid), rule.rule)
        post(coord, url, {"section": "pin_reset"})
        with self.app.app_context():
            self.assertTrue(check_password_hash(User.query.filter_by(phone="+261340000107").first().pin_hash, "1234"))
        post(coord, url, {"section": "pin_reset", "checked": "1"})
        with self.app.app_context():
            self.assertFalse(check_password_hash(User.query.filter_by(phone="+261340000107").first().pin_hash, "1234"))
            for p in (phone, "+261340000107"):
                User.query.filter_by(phone=p).first().pin_hash = generate_password_hash("1234")
            db.session.commit()
        self.assertRegex(self.sms_to("+261340000107")[-1], r"(Temporary PIN|PIN temporaire|PIN vonjimaika) ?: \d{4}")  # in the household's language

    def test_56_instant_language_fetch_and_branded_receipt(self):
        c = A.app.test_client()
        r = c.get("/", headers={"X-CWAS-Lang": "fr"})
        self.assertIn('lang="fr"', r.get_data(as_text=True))
        self.assertNotIn("cwas_lang", r.headers.get("Set-Cookie", ""))  # the quiet fetch stores nothing
        self.assertIn('lang="en"', c.get("/").get_data(as_text=True))
        import barcode128
        bits = barcode128.modules("CW-4F7A9C21")
        self.assertTrue(bits.startswith("11010010000") and bits.endswith("1100011101011"))
        self.assertEqual(len(bits), 11 * (11 + 3) + 2)
        cc = login("+261340000106", DEMO_PW)
        found = re.search(r'data-receipt="(/app/bookings/(CW-[0-9A-F]{8})/receipt)"', cc.get("/app/bookings").get_data(as_text=True))
        page = cc.get(found.group(1)).get_data(as_text=True)
        for part in ('class="c128"', "rc-stamp", "rc-amount", "WINEBALD", found.group(2)):
            self.assertIn(part, page)
        self.assertIn("Tongasoa eto amin\\u0027ny CWAS/Welcome to CWAS/Bienvenue sur CWAS", c.get("/").get_data(as_text=True))  # the phones show the real welcome
