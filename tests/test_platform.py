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
        self.assertIn("Your balance is 5,000 MGA", out)
        book = self.play(phone, "2", "2", "1", "2", "1", "1", "1", "1234")
        self.assertTrue(book.startswith("END Booked!"), book)
        ref = re.search(r"Ref: (CW-[0-9A-F]{8})", book).group(1)
        self.assertIn("Status: pending approval", book)
        self.assertIn(ref, self.play(phone, "2", "3"))
        self.assertIn("Confirm booking", self.play(phone, "2", "2", "1", "2", "1", "1"))
        cancelled = self.play(phone, "2", "4", "1", "1", "1234")
        self.assertIn(f"Cancelled {ref}. Eligible payment was refunded once.", cancelled)
        self.assertIn("Your balance is 5,000 MGA", self.play(phone, "2", "5") + cancelled)
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
        for loud in ("Booked CW-", f"Booking {ref} was cancelled"):  # a booking waiting for approval and a refund are texted
            self.assertIn(loud, bodies)
        self.assertNotIn("Language set", bodies)  # a language change stays in the app

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
        for expect in ("BAL, SOURCES", "Your balance is", "Forage", "recorded: +3,000 MGA", "Booked CW-"):
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

    # ── legal, consent, account deletion ──
    def test_60_legal_pages_exist_and_registration_needs_consent(self):
        c = A.app.test_client()
        for p, needle in (("/terms", "Booking water"), ("/privacy", "Your choices and rights"), ("/refunds", "Automatic refunds")):
            self.assertIn(needle, c.get(p).get_data(as_text=True))
        data = {"name": "No Consent", "phone": "0340007600", "password": "Str0ng Pass #2026", "family_size": "3"}
        r = post(c, "/register", data)
        self.assertEqual(r.status_code, 200)
        with self.app.app_context():
            self.assertIsNone(User.query.filter_by(phone="+261340007600").first())

    def test_61_delete_account_erases_personal_data_and_flags_refund(self):
        c = A.app.test_client()
        r = post(c, "/register", {"name": "Erase Me", "phone": "0340007601", "email": "erase@example.com", "password": "Str0ng Pass #2026", "family_size": "3", "village": "Ampotaka", "distance": "350", "pin": "2580", "recovery": "258025", "accept": "1"})
        self.assertEqual(r.status_code, 302)
        post(c, "/app/wallet", {"provider": "orange", "amount": "2000"})
        export = c.get("/account/export.json").get_json()
        self.assertEqual(export["account"]["name"], "Erase Me")
        self.assertEqual(export["household"]["balance_mga"], 2000)
        post(c, "/api/assistant/message", {"message": "hello"})
        r = post(c, "/account/delete", {"password": "wrong", "confirm": "DELETE", "refund": "1"})
        self.assertEqual(r.status_code, 200)
        r = post(c, "/account/delete", {"password": "Str0ng Pass #2026", "confirm": "DELETE"})
        self.assertEqual(r.status_code, 200)  # balance needs the refund acknowledgement
        r = post(c, "/account/delete", {"password": "Str0ng Pass #2026", "confirm": "DELETE", "refund": "1"})
        self.assertEqual(r.status_code, 302)
        with self.app.app_context():
            self.assertIsNone(User.query.filter_by(phone="+261340007601").first())
            u = User.query.filter_by(name=f"Deleted user {export.get('id', 0)}").first() or User.query.filter(User.name.like("Deleted user %"), User.is_active_flag.is_(False)).order_by(User.id.desc()).first()
            self.assertIsNotNone(u)
            self.assertIsNone(u.email)
            self.assertEqual(u.household.balance, 0)
            self.assertEqual(u.household.name, "Deleted household")
            self.assertTrue(S.reconcile_wallet(u.household))
            self.assertEqual(WalletTxn.query.filter_by(household_id=u.household.id, kind="adjustment").count(), 1)
            from models import ChatThread, Notification
            self.assertEqual(ChatThread.query.filter_by(user_id=u.id).count(), 0)
            self.assertEqual(Notification.query.filter_by(user_id=u.id).count(), 0)
            coord = User.query.filter_by(email="coordinator@cwas.demo").first()
            self.assertTrue(any("is owed" in S.render_notification(n, "en") for n in Notification.query.filter_by(user_id=coord.id)))
        r = post(c, "/login", {"identifier": "erase@example.com", "password": "Str0ng Pass #2026"})
        self.assertEqual(r.status_code, 200)

    # ── assistant: saved chats, every kind of file, deletion ──
    def test_70_assistant_chats_files_and_deletion(self):
        import io
        import zipfile
        c = login("+261340000102", DEMO_PW)
        hdr = {"X-CSRFToken": token(c)}
        r = c.post("/api/assistant/message", data={"message": "balance"}, headers=hdr)
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        tid = d["thread"]["id"]
        self.assertIn("MGA", d["assistant"]["body"])
        png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + (7).to_bytes(4, "big") + (5).to_bytes(4, "big") + b"\x08\x06\x00\x00\x00" + b"\x00" * 20
        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w") as z:
            z.writestr("a.txt", "hello")
            z.writestr("b.txt", "world")
        files = [("files", (io.BytesIO(png), "pic.png")), ("files", (io.BytesIO(b"%PDF-1.4\n/Type /Page\n/Type /Page\n"), "doc.pdf")),
                 ("files", (io.BytesIO(b"name,amount\nRasoa,1000\nRakoto,2500\n"), "sheet.csv")), ("files", (io.BytesIO(zbuf.getvalue()), "bundle.zip")),
                 ("files", (io.BytesIO(b"MZ\x90\x00binary"), "setup.exe"))]
        r = c.post("/api/assistant/message", data={"message": "my payment receipt", "thread_id": str(tid), "files": [f[1] for f in files]}, headers=hdr, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        d = r.get_json()
        reply = d["assistant"]["body"]
        for expect in ("I received 5 file(s)", "7 x 5 pixels", "about 2 pages", "2 rows and 2 columns", "total 3,500", "Archive with 2 items", "never opened or run", "transaction reference"):
            self.assertIn(expect, reply)
        self.assertEqual(len(d["user"]["files"]), 5)
        img, exe = d["user"]["files"][0], d["user"]["files"][4]
        r = c.get(img["url"])
        self.assertEqual((r.status_code, r.mimetype), (200, "image/png"))
        self.assertIn("sandbox", r.headers["Content-Security-Policy"])
        r = c.get(exe["url"])
        self.assertEqual(r.mimetype, "application/octet-stream")
        self.assertIn("attachment", r.headers["Content-Disposition"])
        r = c.post("/api/assistant/message", data={"message": "", "thread_id": str(tid), "files": [(io.BytesIO(b"x" * (10 * 1024 * 1024 + 1)), "big.bin")]}, headers=hdr, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 413)
        other = login("+261340000101", DEMO_PW)
        self.assertEqual(other.get(img["url"]).status_code, 404)
        self.assertEqual(other.get(f"/api/assistant/threads/{tid}").status_code, 404)
        self.assertEqual(other.post(f"/api/assistant/threads/{tid}/delete", headers={"X-CSRFToken": token(other)}).status_code, 404)
        self.assertEqual(c.post(f"/api/assistant/threads/{tid}/rename", json={"title": "My chat"}, headers=hdr).get_json()["title"], "My chat")
        self.assertIn("balance", c.get(f"/app/assistant/{tid}/export.txt").get_data(as_text=True))
        self.assertIn("My chat", c.get("/app/assistant").get_data(as_text=True))
        self.assertEqual(len(c.get(f"/api/assistant/threads/{tid}").get_json()["messages"]), 4)
        self.assertEqual(c.post(f"/api/assistant/threads/{tid}/delete", headers=hdr).status_code, 200)
        self.assertEqual(c.get(img["url"]).status_code, 404)
        self.assertEqual(c.get(f"/api/assistant/threads/{tid}").status_code, 404)
        c.post("/api/assistant/message", data={"message": "one"}, headers=hdr)
        c.post("/api/assistant/message", data={"message": "two", "thread_id": ""}, headers=hdr)
        self.assertEqual(c.post("/api/assistant/threads/delete-all", headers=hdr).status_code, 200)
        self.assertNotIn("data-thread=", c.get("/app/assistant").get_data(as_text=True))

    # ── receipts, live notifications ──
    def test_75_receipt_preview_and_notification_poll(self):
        c = login("+261340000106", DEMO_PW)
        found = re.search(r'data-receipt="(/app/bookings/(CW-[0-9A-F]{8})/receipt)"', c.get("/app/bookings").get_data(as_text=True))
        self.assertIsNotNone(found)
        frag = c.get(f"{found.group(1)}?partial=1").get_data(as_text=True)
        self.assertIn("data-printer", frag)
        self.assertNotIn("<html", frag)
        d = c.get("/api/notifications/poll?after=0").get_json()
        self.assertIn("items", d)
        for _ in range(20):  # the poll answers in batches: read up to the newest first
            more = c.get(f"/api/notifications/poll?after={d['last']}").get_json()
            if not more["items"]:
                break
            d = more
        self.assertEqual(c.get(f"/api/notifications/poll?after={d['last']}").get_json()["items"], [])
        with self.app.app_context():
            S.event(User.query.filter_by(phone="+261340000106").first(), "Water collected for booking {ref}. Thank you!", "booking", ref="CW-TESTTEST")
            db.session.commit()
        new = c.get(f"/api/notifications/poll?after={d['last']}").get_json()
        self.assertEqual(len(new["items"]), 1)
        self.assertIn("CW-TESTTEST", new["items"][0]["text"])

    # ── media: each asset once, loading at once, nothing unrelated shipped ──
    def test_80_every_media_asset_has_one_home_and_loads_at_once(self):
        c = A.app.test_client()
        root = os.path.dirname(os.path.abspath(A.__file__))
        seen, foot, pages = [], set(), {}
        for p in ("/", "/platform", "/access", "/water-points", "/about", "/terms", "/privacy", "/refunds", "/simulator", "/login"):
            html = pages[p] = c.get(p).get_data(as_text=True)
            main, _, tail = html.partition("<footer")
            self.assertNotIn('loading="lazy"', main)
            found = re.findall(r'<(?:video|img)[^>]* (?:data-)?src="(/static/(?:media|img/photos)/[^"?]+)(?:\?v=[0-9a-f]+)?"', main)  # a film further down names its file in data-src
            if p == "/login":  # the sign-up and log-in pages show the homepage's live-water pond on purpose
                self.assertIn("/static/media/hero/hero-base-land.webp", found)
                found = [f for f in found if "hero-base" not in f]
            seen += found
            foot.update(re.findall(r'<img[^>]* src="(/static/img/photos/[^"?]+)(?:\?v=[0-9a-f]+)?"', tail))
        self.assertEqual(len(seen), len(set(seen)), seen)
        self.assertGreaterEqual(len(seen), 10)
        self.assertEqual(foot, {"/static/img/photos/spiny-baobabs-960.webp"})
        for src in set(seen) | foot:
            self.assertTrue(os.path.exists(os.path.join(root, src.lstrip("/"))), src)
        # every shipped photograph is registered and used; the unrelated ones are gone
        from pathlib import Path
        media = (Path(root) / "templates" / "partials" / "media.html").read_text()
        for f in (Path(root) / "static" / "img" / "photos").glob("*.webp"):
            self.assertIn("'" + f.stem.rsplit("-", 1)[0] + "'", media, f.name)
        templates = "".join(t.read_text() for t in (Path(root) / "templates").rglob("*.html"))
        for gone in ("water-road", "baobab-path", "spiny-forest", "zebu-carts", "hero-source", "hero-pay", "forward-land", "data-hero-state"):
            self.assertNotIn(gone, templates, gone)
        self.assertEqual(sorted(p.name for p in (Path(root) / "static" / "media" / "hero").iterdir()), ["hero-base-land.avif", "hero-base-land.webp", "hero-base-port.avif", "hero-base-port.webp"])
        home = pages["/"]
        self.assertEqual(home.count('class="dk-card"'), 12)
        for hook in ("data-typeline", "data-orbit", "data-tunnel", "data-hero-media", "tel:*384*9411%23", "js/hero.js", "data-hero-liquid", "data-phones",
                     "data-gallery-tunnel", "js/home.js", "Winebald Max 9 Pro", "WINEBALD", "Book your slot. Skip the queue.", "borehole-windmill", "pilot/follow", "foot-big"):
            self.assertIn(hook, home)
        for gone in ("field research", "flagfield", "pexels", "Follow the water", "Pilot 2026"):
            self.assertNotIn(gone, home)
        # the hot-linked films always carry a local poster, so a page is never blank if Pexels is unreachable
        for p in ("/about",):  # /platform shows real product screens instead of a film
            v = re.search(r'<video[^>]*src="https://videos\.pexels\.com[^"]+"[^>]*>', pages[p]) or re.search(r'<video[^>]*poster="/static/[^"]+"[^>]*src="https://videos\.pexels\.com', pages[p])
            self.assertTrue(v, p)
            self.assertIn('poster="/static/img/photos/', pages[p])
        self.assertIn("data-phones", pages["/access"])
        self.assertNotIn("<img", pages["/water-points"].partition("<footer")[0].partition("<main")[2])

    # ── themes: logo, favicon and app icons follow the theme ──
    def test_81_theme_brand_files(self):
        root = os.path.dirname(os.path.abspath(A.__file__))
        self.assertIn('data-theme="saina"', A.app.test_client().get("/").get_data(as_text=True))  # a first visit gets the Default theme
        for th, colour in (("saina", "#FFFFFF"), ("fotsy", "#FFFFFF"), ("maitso", "#007E3A"), ("mena", "#D42A20")):
            c = A.app.test_client()
            c.set_cookie("cwas_visit_theme", th)
            html = c.get("/").get_data(as_text=True)
            self.assertIn(f"img/brand/favicon-{th}.svg", html)
            self.assertIn(f'name="theme-color" content="{colour}"', html)
            m = c.get("/manifest.webmanifest").get_json()
            self.assertEqual(m["theme_color"], colour)
            for icon in m["icons"]:
                self.assertTrue(os.path.exists(os.path.join(root, icon["src"].lstrip("/"))), icon["src"])
            for f in ("favicon-32", "apple-touch-icon"):
                self.assertTrue(os.path.exists(os.path.join(root, "static", "icons", th, f + ".png")))

    # ── household needs: the same questions on the web and by USSD; the subsidy waits for a coordinator ──
    def test_82_needs_on_web_wait_for_a_coordinator(self):
        c = A.app.test_client()
        base = {"name": "Needs Web", "phone": "0340007801", "password": "Str0ng Pass #2026", "family_size": "5", "village": "Ampotaka", "accept": "1", "pin": "2468", "recovery": "246824"}
        r = post(c, "/register", dict(base, vuln=["elderly", "disability", "nonsense"], distance="1500"))
        self.assertEqual(r.status_code, 302)
        with self.app.app_context():
            h = User.query.filter_by(phone="+261340007801").first().household
            self.assertEqual((h.vuln_flags, h.distance_m, h.priority_level, h.needs_review), ("elderly,disability", 1500, "standard", True))
            score, parts = S.priority_breakdown(h)
            self.assertEqual(parts[0], ("Vulnerability level (self-reported, awaiting check)", 45))
            src = WaterSource.query.first()
            self.assertEqual(S.price_quote(h, src, 100)[1], 0)  # no subsidy before the check
            hid = h.id
        coord = login("coordinator@cwas.demo", DEMO_PW)
        post(coord, f"/coord/households/{hid}", {"section": "priority", "priority": "high", "distance": "1500", "vuln": ["elderly", "disability"]})
        with self.app.app_context():
            h = db.session.get(Household, hid)
            self.assertEqual((h.priority_level, h.needs_review), ("high", False))
            self.assertGreater(S.price_quote(h, WaterSource.query.first(), 100)[1], 0)
        # a household without a distance or a PIN is asked for them
        c2 = A.app.test_client()
        r = post(c2, "/register", dict(base, phone="0340007802", pin="", distance=""))
        self.assertEqual(r.status_code, 200)
        page = r.get_data(as_text=True)
        self.assertIn("Choose how far your household is from water.", page)
        self.assertIn("Create a 4-digit PIN.", page)

    def test_83_needs_by_ussd_match_the_web(self):
        phone = "+261340007803"
        out = self.play(phone, "2", "1", "Lala Ussd", "Ampotaka", "6")
        self.assertIn("1. Aged 60+", out)
        self.assertIn("Invalid choice", self.play(phone, "2", "1", "Lala Ussd", "Ampotaka", "6", "77"))
        out = self.play(phone, "2", "1", "Lala Ussd", "Ampotaka", "6", "31", "4", "2", "1357", "1357", "112244", "112244")
        self.assertNotIn("I agree to the Terms of Service", out)  # no consent screen on USSD: the account opens at once
        self.assertTrue(out.startswith("END Welcome Lala!"), out)
        with self.app.app_context():
            h = User.query.filter_by(phone=phone).first().household
            second = S.operational_sources()[1].id
            self.assertEqual((h.vuln_flags, h.distance_m, h.home_source_id, h.needs_review), ("elderly,infant", 1500, second, True))
        for lang in ("1", "3"):
            self.assertLessEqual(len(self.play("+261340007804", lang, "1", "Test Mg", "Ampotaka", "3")) - 4, 182)

    def test_83b_welcome_layout_and_other_water_point(self):
        out = self.ussd("+261340007901", "", sid="layout-1")
        self.assertEqual(out, "CON Tongasoa eto amin'ny CWAS/Welcome to CWAS/Bienvenue sur CWAS\n\nSafidio ny fiteny/Choose language/Choisissez votre langue:\n\n1. Malagasy\n2. English\n3. Francais\n\n99. Exit")
        with self.app.app_context():
            n = len(S.operational_sources())
        menu = self.play("+261340007901", "2", "1", "Voahangy Other", "Ampotaka", "3", "0", "2")
        self.assertIn(f"{n + 1}. Other", menu)
        self.assertIn(f"{n + 2}. Not sure", menu)
        out = self.play("+261340007901", "2", "1", "Voahangy Other", "Ampotaka", "3", "0", "2", str(n + 1), "Puits Nord", "1357", "1357", "112255", "112255")
        self.assertTrue(out.startswith("END Welcome Voahangy!"), out)
        c = A.app.test_client()
        r = post(c, "/register", {"name": "Web Other", "phone": "0340007902", "password": "Str0ng Pass #2026", "family_size": "3", "village": "Ampotaka", "accept": "1",
                                  "pin": "2468", "recovery": "246824", "distance": "350", "home_source": "other", "home_other": "Puits Nord"})
        self.assertEqual(r.status_code, 302)
        with self.app.app_context():
            for phone in ("+261340007901", "+261340007902"):
                h = User.query.filter_by(phone=phone).first().household
                self.assertEqual((h.home_source_id, h.home_source_note), (None, "Puits Nord"))
            # a new water point appears in the registration list at once
            db.session.add(WaterSource(name="Forage Nouveau", village="Ampotaka"))
            db.session.commit()
        self.assertIn("Forage Nouveau", A.app.test_client().get("/register").get_data(as_text=True))
        self.assertIn("Forage Nouveau", self.play("+261340007903", "2", "1", "New Point", "Ampotaka", "3", "0", "2"))
        with self.app.app_context():
            WaterSource.query.filter_by(name="Forage Nouveau").delete()
            db.session.commit()

    def test_84_pilot_news(self):
        c = A.app.test_client()
        r = post(c, "/pilot/follow", {"email": "news@example.org", "next": "/about"})
        self.assertEqual(r.headers["Location"], "/about")
        post(c, "/pilot/follow", {"email": "news@example.org", "next": "//evil.example"})
        self.assertEqual(post(c, "/pilot/follow", {"email": "bad", "next": "/"}).status_code, 302)
        with self.app.app_context():
            from models import PilotFollower
            rows = PilotFollower.query.filter_by(email="news@example.org").all()
            self.assertEqual(len(rows), 1)
            tok = rows[0].token
        self.assertEqual(A.app.test_client().get("/admin/pilot-followers.csv").status_code, 302)
        c.get(f"/pilot/leave/{tok}")
        with self.app.app_context():
            from models import PilotFollower
            self.assertIsNone(PilotFollower.query.filter_by(email="news@example.org").first())

    # ── the website's demo phones show exactly what the engine answers ──
    def test_85_demo_phones_match_the_engine(self):
        import ussd as U
        with A.app.test_request_context("/"):
            d = U.demo_script("en")
            with self.app.app_context():
                u = User.query.filter_by(phone="+261340000108").first()
                first = S.first_name(u.name)
        screens = [a for op, a in d["nova"] if op in ("screen", "end")]
        live = self.play("+261340000108", "2")
        self.assertEqual(live[4:], screens[1].replace("Rasoa", first))
        self.assertEqual(self.ussd("+261340000108", "", sid="demo-welcome")[4:], screens[0])
        live = self.play("+261340000108", "2", "2")
        self.assertEqual(live[4:], screens[2])
        book = next(a for op, a in d["max"] if op == "type" and a.startswith("BOOK"))
        with self.app.app_context():
            reply = U.handle_sms("+261340000108", book)
            db.session.commit()
        expect = next(a for op, a in d["max"] if op == "in" and a.startswith("Booked"))
        self.assertRegex(reply, r"^Booked CW-[0-9A-F]{8}: ")
        same = lambda m: m.split(": ", 1)[1].rsplit(", ", 1)[0]  # noqa: E731  water point, date, time and litres; the price follows the household
        self.assertEqual(same(reply), same(expect))
        self.assertTrue(reply.endswith("Status: pending approval."), reply)
        # the homepage phones are the device lab's own devices (sim.js) fed with this script
        home = A.app.test_client().get("/").get_data(as_text=True)
        self.assertIn('id="phones-init"', home)
        self.assertIn("js/sim.js", home)
        for key in ("lite", "nova", "max"):
            self.assertIn(f'data-dev="{key}"', home)
        self.assertIn("Enter your 4-digit PIN to confirm", home)

    # ── the committed stylesheet is really built from its source ──
    def test_86_stylesheet_builds_from_source(self):
        root = os.path.dirname(os.path.abspath(A.__file__))
        src = open(os.path.join(root, "static", "css", "input.css"), encoding="utf-8").read()
        depth = 0
        for n, line in enumerate(src.splitlines(), 1):
            depth += line.count("{") - line.count("}")
            self.assertGreaterEqual(depth, 0, f"input.css line {n}: a stray closing brace")
        self.assertEqual(depth, 0, "input.css: a brace is never closed")
        built = open(os.path.join(root, "static", "css", "app.css"), encoding="utf-8").read()
        for marker in (".auth-side", ".os.in-app", ".dial-row", ".pd-rig", ".foot-big", ".logo-tile", "--brand:17 17 17", ".can.can-lg", "--paper:#007E3A", ".need", ".foot-seals", ".role-tabs", ".rg.is-live"):
            self.assertTrue(marker.lower() in built.lower(), f"app.css is older than input.css (missing {marker}); run npm run build:css")

    # ── every page in every language ──
    def test_91_ussd_screens_translated_and_short(self):
        """Walks member and staff screens in all three languages, PIN prompts and My profile included: every screen fits
        in 182 characters and every string is translated."""
        MISSING.clear()
        walked = 0
        member = ["", "1", "1*1", "1*1*1", "1*1*1*1", "1*1*1*1*0", "1*1*1*1*0*2", "2", "2*1", "3", "4", "5", "6", "7", "7*1", "9", "10",
                  "8", "8*1", "8*2", "8*3", "8*4", "8*5", "8*6", "8*7", "8*8", "8*8*1", "8*8*2", "8*9", "98"]
        staff = ["", "1", "1*1", "1*1*1", "2", "3", "4", "5", "5*1", "6", "7", "8", "8*1", "8*4", "8*5", "8*6", "9", "98", "98*11", "98*12"]
        for phone, paths in (("+261340000101", member), ("+261340000001", staff)):
            with self.app.app_context():
                u = User.query.filter_by(phone=phone).first()
                u.pin_failed, u.pin_locked_until = 0, None
                db.session.commit()
            for lang in ("1", "2", "3"):
                for p in paths:
                    out = self.play(phone, lang, *[t for t in p.split("*") if t])
                    walked += 1
                    self.assertTrue(out.startswith(("CON", "END")), (lang, p, out))
        self.assertGreater(walked, 140)
        self.assertEqual(MISSING, set(), sorted(MISSING))

    def test_92_all_pages_all_languages(self):
        MISSING.clear()
        pages = {"member": ("+261340000101", DEMO_PW, ["/app", "/app/book", "/app/bookings", "/app/wallet", "/app/water-points", "/app/notifications", "/app/profile", "/app/assistant", "/account/security", "/account/delete"]),
                 "coord": ("coordinator@cwas.demo", DEMO_PW, ["/coord", "/coord/queue", "/coord/bookings", "/coord/sources", "/coord/maintenance", "/coord/households", "/coord/deposits", "/coord/reports", "/coord/insights", "/coord/announce", "/simulator"]),
                 "admin": ("info@winebald.tech", "Pilot Water #2026x", ["/admin", "/admin/users", "/admin/settings", "/admin/audit", "/admin/database", "/admin/channels"])}
        public = ["/", "/platform", "/access", "/water-points", "/about", "/terms", "/privacy", "/refunds", "/login", "/register", "/forgot", "/forgot-pin", "/simulator"]
        for lang in ("mg", "fr", "en"):
            c = A.app.test_client()
            c.set_cookie("cwas_lang", lang)
            for p in public:
                self.assertEqual(c.get(p).status_code, 200, (lang, p))
            for who, (ident, pw, paths) in pages.items():
                cc = login(ident, pw)
                cc.set_cookie("cwas_lang", lang)
                with self.app.app_context():
                    u = User.query.filter((User.email == ident) | (User.phone == ident)).first()
                    u.language = lang
                    db.session.commit()
                for p in paths:
                    r = cc.get(p)
                    self.assertEqual(r.status_code, 200, (lang, p))
                    self.assertIn(f'lang="{lang}"', r.get_data(as_text=True)[:200])
        self.assertEqual(MISSING, set(), sorted(MISSING))


    # ── sign up on the web: two tabs; a coordinator needs the access code and still waits for an administrator ──
    def test_96_coordinator_web_signup_needs_the_access_code(self):
        c = A.app.test_client()
        page = c.get("/register").get_data(as_text=True)
        for needle in ('class="role-tabs"', 'name="role" value="member" checked', 'name="role" value="coordinator"', 'name="access_code"'):
            self.assertIn(needle, page)
        self.assertNotIn("Community coordinators can skip the household questions.", page)
        base = {"name": "Coord Web", "phone": "0340007991", "password": "Str0ng Pass #2026", "accept": "1", "role": "coordinator"}
        self.assertIn("Enter the coordinator access code.", post(c, "/register", base).get_data(as_text=True))
        self.assertIn("That code is not valid.", post(c, "/register", dict(base, access_code="Coord@2025")).get_data(as_text=True))
        with self.app.app_context():
            self.assertIsNone(User.query.filter_by(phone="+261340007991").first())
        self.assertEqual(post(c, "/register", dict(base, access_code="Coord@2026")).status_code, 302)
        with self.app.app_context():
            u = User.query.filter_by(phone="+261340007991").first()
            self.assertEqual((u.role, u.is_active_flag, u.household), ("coordinator", False, None))

    # ── device lab: every guided run is planned against the live data, so it finishes, and again when repeated ──
    def test_97_guided_runs_finish(self):
        c = login("coordinator@cwas.demo", DEMO_PW)
        self.assertNotIn(">Naly</button>", c.get("/simulator").get_data(as_text=True))  # demo households only
        for kind, want in (("register", "END Welcome Winebald!"), ("deposit", "END Deposit recorded"), ("book", "END Booked!"),
                           ("approve", "END Approved"), ("book", "END Booked!"), ("deposit", "END Deposit recorded")):
            plan = c.get(f"/simulator/api/tour/{kind}").get_json()
            self.assertTrue(self.play(plan["phone"], *plan["steps"]).startswith(want), (kind, plan))
        self.assertEqual(c.get("/simulator/api/tour/nope").status_code, 404)

    # ── /access shows the household menu exactly as the USSD engine draws it, in every language ──
    def test_98_access_menu_is_the_engines(self):
        from markupsafe import escape
        import ussd as U
        for lang in ("en", "fr", "mg"):
            page = A.app.test_client().get(f"/access?lang={lang}").get_data(as_text=True)
            with self.app.app_context():
                screens = U.household_menu_screens(lang)
            for screen in screens:
                self.assertIn(str(escape(screen)), page)
        self.assertIn("Deposit funds", U.household_menu_screens("en")[0])

    # ── search engines, security headers and the compliance seals ──
    def test_95_seo_security_and_seals(self):
        c = A.app.test_client()
        home = c.get("/").get_data(as_text=True)
        for needle in ('rel="canonical"', 'hreflang="fr"', 'hreflang="mg"', 'hreflang="x-default"', 'property="og:image"',
                       'name="twitter:card"', '"Organization"', '"FAQPage"', 'class="foot-seals', 'ISO/IEC 27001', 'SOC 2', 'index, follow'):
            self.assertIn(needle, home)
        self.assertNotIn("Privacy and security frameworks we follow", home)
        fr = c.get("/platform?lang=fr").get_data(as_text=True)
        self.assertIn('<html lang="fr"', fr)
        self.assertRegex(fr, r'rel="canonical" href="[^"]*/platform\?lang=fr"')
        self.assertIn("Disallow: /admin", c.get("/robots.txt").get_data(as_text=True))
        sm = c.get("/sitemap.xml")
        self.assertEqual(sm.status_code, 200)
        self.assertIn('hreflang="mg"', sm.get_data(as_text=True))
        self.assertIn("Contact: mailto:", c.get("/.well-known/security.txt").get_data(as_text=True))
        login = c.get("/login")
        self.assertIn("noindex", login.headers.get("X-Robots-Tag", ""))
        self.assertIn('content="noindex, nofollow"', login.get_data(as_text=True))
        csp = login.headers["Content-Security-Policy"]
        for part in ("script-src 'self'", "frame-ancestors 'none'", "object-src 'none'", "base-uri 'self'"):
            self.assertIn(part, csp)
        self.assertNotIn("images.pexels.com", csp)
        self.assertEqual(login.headers["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertEqual(c.get("/app").headers.get("Cache-Control"), "no-store")

    # ── navigation, links, footer chips and the product screens of every theme ──
    def test_99_nav_links_footer_and_theme_screens(self):
        from pathlib import Path
        root = Path(os.path.dirname(os.path.abspath(A.__file__)))
        c = A.app.test_client()
        home = c.get("/").get_data(as_text=True)
        head = home.partition("</header>")[0]
        # Platform and Access: the word is a link to its page, the chevron beside it is the button that opens the menu
        for ep in ("platform", "access"):
            self.assertRegex(head, r'<div class="nav-item" data-menu><a class="pr-1\.5" href="/%s">' % ep)
            self.assertRegex(head, r'<button type="button" class="nav-caret" data-menu-btn aria-expanded="false" aria-controls="nav-%s" aria-label="[^"]+">' % ep)
            self.assertIn('id="nav-%s"' % ep, head)
            self.assertIn('aria-controls="sheet-%s"' % ep, home)
            self.assertIn('id="sheet-%s"' % ep, home)
        self.assertNotIn("aria-haspopup", head)
        for path, ep in (("/platform", "platform"), ("/access", "access"), ("/simulator", "access")):
            h = c.get(path).get_data(as_text=True).partition("</header>")[0]
            self.assertIn('<div class="nav-item is-active" data-menu><a class="pr-1.5" href="/%s"' % ep, h)
        self.assertIn('href="/platform" aria-current="page"', c.get("/platform").get_data(as_text=True).partition("</header>")[0])
        # links carry no underline anywhere: one link rule, no underline utilities (the browser's abbr default aside)
        css = (root / "static" / "css" / "app.css").read_text()
        for m in re.finditer(r"text-decoration(?:-line)?:\s*underline", css):
            self.assertIn("abbr", css[max(0, m.start() - 80):m.start()])
        self.assertNotIn("underline", (root / "static" / "css" / "input.css").read_text().replace("no-underline", ""))
        for t in (root / "templates").rglob("*.html"):
            self.assertIsNone(re.search(r"(?<![\w-])(?:hover:|focus:)?underline(?![\w-])", t.read_text()), t.name)
        # footer: the dial chips carry their icons again
        foot = home.partition("<footer")[2]
        self.assertEqual(foot.count('class="foot-chip"'), 2)
        self.assertRegex(foot, r'<a class="foot-chip" href="tel:[^"]+"><svg')
        self.assertRegex(foot, r'<a class="foot-chip" href="sms:[^"]+"><svg')
        self.assertIn("&copy; 2026 Winebald Technologies. All rights reserved.", foot)
        # every static file a template names exists (the wallet once pointed at payment logos that did not)
        for t in (root / "templates").rglob("*.html"):
            for f in re.findall(r"url_for\('static', filename='([^'~]+)'\)", t.read_text()):
                self.assertTrue((root / "static" / f).exists(), f"{t.name}: {f}")
        # product screens: one set per theme; the page shows the current theme's set and carries the others for a live swap
        themes = ("saina", "fotsy", "maitso", "mena")
        for th in themes:
            for key, widths in (("coord-queue", (960, 1800)), ("coord-insights", (960, 1800)), ("app-book", (600, 1170)), ("app-wallet", (600, 1170))):
                for w in widths:
                    for ext in ("webp", "avif"):
                        self.assertTrue((root / "static" / "img" / "shots" / th / f"{key}-{w}.{ext}").exists(), (th, key, w, ext))
            c.set_cookie("cwas_visit_theme", th)
            html = c.get("/platform").get_data(as_text=True)
            self.assertEqual(len(re.findall(r'<img data-shot src="/static/img/shots/%s/' % th, html)), 4, th)
            for other in themes:
                self.assertEqual(html.count('data-shot-%s="' % other), 8)  # the AVIF source and the WebP image
            self.assertEqual(html.count('<source type="image/avif" data-shot srcset="/static/img/shots/%s/' % th), 4)
        c.delete_cookie("cwas_visit_theme")
        self.assertEqual(sorted(p.name for p in (root / "static" / "img" / "shots").iterdir()), sorted(themes))

    # ── default theme per visit, the FAQ links, the statistics below the hero, fast media and lighter uploads ──
    def test_100_theme_faq_stats_media_and_uploads(self):
        import gzip, io
        from pathlib import Path
        from PIL import Image
        import uploads as UP
        root = Path(os.path.dirname(os.path.abspath(A.__file__)))
        c = A.app.test_client()
        # every visit opens in Default; a picked theme lives in a cookie that ends with the visit, and the old year-long one is cleared
        self.assertIn('data-theme="saina"', c.get("/").get_data(as_text=True)[:200])
        c.set_cookie("cwas_theme", "mena")
        r = c.get("/"); self.assertIn('data-theme="saina"', r.get_data(as_text=True)[:200])
        self.assertTrue(any(h.startswith("cwas_theme=;") for h in r.headers.getlist("Set-Cookie")))
        c.delete_cookie("cwas_theme"); c.set_cookie("cwas_visit_theme", "mena")
        self.assertIn('data-theme="mena"', c.get("/").get_data(as_text=True)[:200]); c.delete_cookie("cwas_visit_theme")
        js = (root / "static" / "js" / "app.js").read_text()
        line = next(l for l in js.splitlines() if "document.cookie = `cwas_visit_theme" in l)
        self.assertNotIn("max-age", line); self.assertNotIn("cwas_theme=", js)
        # the FAQ names the three policies and links each one; search engines get the plain sentence
        home = c.get("/").get_data(as_text=True)
        self.assertIn('See the <a class="lnk" href="/terms">Terms of Service</a>, <a class="lnk" href="/privacy">Privacy Policy</a> and <a class="lnk" href="/refunds">Refund Policy</a>.', home)
        self.assertIn('"See the Terms of Service, Privacy Policy and Refund Policy."', home)
        self.assertNotIn("linked in the footer", home); self.assertNotIn("{terms}", home)
        # the four statistics sit in their own section after the hero, not over it
        hero, _, rest = home.partition("</section>")
        self.assertNotIn("89%", hero); self.assertIn("89%", rest.partition("</section>")[0]); self.assertNotIn("-mt-10", rest.partition("</section>")[0])
        # films: the homepage one waits for the view with its poster showing; the one opening About loads at once
        self.assertRegex(home, r'<video class="vid"[^>]* preload="none" poster="/static/media/toliara-poster\.webp[^"]*" data-src="/static/media/toliara-sd\.mp4')
        self.assertRegex(c.get("/about").get_data(as_text=True), r'<video class="vid"[^>]* preload="auto" poster="[^"]+" src="https://videos\.pexels\.com')
        # static files: versioned copies are kept for a year, text is gzipped, the hero has AVIF first
        self.assertIn('<source type="image/avif" srcset="/static/media/hero/hero-base-land.avif?v=', home)
        css = re.search(r'href="(/static/css/app\.css\?v=[0-9a-f]+)"', home).group(1)
        r = c.get(css, headers={"Accept-Encoding": "gzip"})
        self.assertEqual(r.headers["Cache-Control"], "public, max-age=31536000, immutable"); self.assertEqual(r.headers["Content-Encoding"], "gzip")
        self.assertEqual(gzip.decompress(r.data), (root / "static" / "css" / "app.css").read_bytes())
        self.assertEqual(c.get("/static/css/app.css").headers["Cache-Control"], "public, max-age=86400")
        self.assertNotIn("Content-Encoding", c.get("/", headers={"Accept-Encoding": "gzip"}).headers)  # pages are never compressed (BREACH)
        # uploads: a big phone photo comes out lighter, HD, upright and without camera metadata; other files are untouched
        exif = Image.Exif(); exif[0x010F] = "PhoneMaker"; exif[0x0112] = 6
        buf = io.BytesIO(); Image.linear_gradient("L").resize((4000, 3000)).convert("RGB").save(buf, "JPEG", quality=97, exif=exif.tobytes())
        out = UP.optimize("photo.jpg", buf.getvalue()); im = Image.open(io.BytesIO(out))
        self.assertLess(len(out), len(buf.getvalue())); self.assertEqual(im.format, "JPEG"); self.assertEqual(im.size, (1920, 2560))
        self.assertIsNone(im.getexif().get(0x010F))
        pdf = b"%PDF-1.4\n%fake\n"; self.assertEqual(UP.optimize("doc.pdf", pdf), pdf)
        self.assertIn("Pillow==", (root / "requirements.txt").read_text())

    # ── device lab: the dial code always ends with #, it opens the dialer, and guided runs follow SEED_DEMO ──
    def test_101_device_lab_dial_code_and_guided_runs(self):
        import services as S, ussd as U
        for raw in ("*384*9411", "*384*9411#", ' "*384*9411#" ', "'*384*9411'", "", None):
            self.assertEqual(S.ussd_code(raw), "*384*9411#", raw)
        self.assertTrue(S.DIAL.endswith("#")); self.assertEqual(U.DIAL, S.DIAL)
        for f in ("app.py", "web.py", "ussd.py", "services.py"):  # one reader of the raw variable: services.ussd_code
            src = open(os.path.join(os.path.dirname(os.path.abspath(A.__file__)), f)).read()
            self.assertEqual(src.count('environ.get("AT_USSD_CODE")'), 1 if f == "services.py" else 0, f)
        c = A.app.test_client()
        lab = c.get("/simulator").get_data(as_text=True)
        self.assertIn('<a class="chip chip-link" href="tel:*384*9411%23"', lab); self.assertIn('data-dial="*384*9411#"', lab)
        self.assertNotIn('href="tel:*384*9411"', lab)
        was = A.app.config["DEMO_DATA"]
        try:
            A.app.config["DEMO_DATA"] = True
            self.assertIn("data-tour=", c.get("/simulator").get_data(as_text=True))
            r = c.get("/simulator/api/tour/deposit"); self.assertEqual(r.status_code, 200); self.assertTrue(r.get_json()["steps"])
            A.app.config["DEMO_DATA"] = False
            self.assertNotIn("data-tour=", c.get("/simulator").get_data(as_text=True))
            self.assertEqual(c.get("/simulator/api/tour/deposit").status_code, 404)
        finally:
            A.app.config["DEMO_DATA"] = was
        self.assertIn("warmShots", (A.app.static_folder and open(os.path.join(A.app.static_folder, "js", "app.js")).read()))

    # ── homepage gallery tunnel on phones: one canvas instead of 3D layers, a light photo set for data savers ──
    def test_102_gallery_tunnel_on_phones(self):
        import json
        from pathlib import Path
        root = Path(os.path.dirname(os.path.abspath(A.__file__)))
        home = A.app.test_client().get("/").get_data(as_text=True)
        small = json.loads(re.search(r"data-images-small='([^']+)'", home).group(1))
        self.assertGreaterEqual(len(small), 6)
        for u in small:
            self.assertIn("-480.webp", u); self.assertTrue((root / u.split("?")[0].lstrip("/")).exists(), u)
        js = (root / "static" / "js" / "home.js").read_text()
        self.assertIn('matchMedia("(max-width: 767px), (pointer: coarse)")', js); self.assertIn('cv.className = "gt-canvas"', js)
        self.assertIn(".gt-canvas{", (root / "static" / "css" / "input.css").read_text())

    def test_103_sms_wording_phone_picker_pin_sign_in_and_admin(self):
        from flask import url_for
        from models import Notification
        from web import external_url
        root = os.path.dirname(os.path.abspath(A.__file__))
        read = lambda p: open(os.path.join(root, p), encoding="utf-8").read()
        # SMS: no CWAS SMS label, balances as sentences; no USSD administrator code left anywhere
        for f in ("translations.py", "ussd.py", "services.py"):
            self.assertNotIn("CWAS SMS", read(f)); self.assertNotIn("enroll_admin", read(f))
        self.assertIn('"Your balance is {balance}"', read("ussd.py"))
        # phone numbers are checked against the country picked beside them; Madagascar is first and every row has a flag
        self.assertEqual(S.parse_phone("034 12 345 67", "MG"), "+261341234567")
        self.assertEqual(S.parse_phone("06 12 34 56 78", "FR"), "+33612345678")
        self.assertEqual(S.parse_phone("+261 34 12 345 67", "FR"), "+261341234567")
        self.assertEqual(S.parse_phone("12", "MG"), "")
        rows = S.phone_countries()
        self.assertEqual(rows[0][:2], ["MG", 261]); self.assertGreater(len(rows), 200)
        self.assertTrue(all(os.path.exists(os.path.join(root, "static", "flags", r[0].lower() + ".svg")) for r in rows))
        c = self.app.test_client()
        page = c.get("/login").get_data(as_text=True)
        for s in ("data-phone", "data-either", "flags/mg.svg", "js/phone.js", 'name="cc" value="MG"', "USSD PIN"):
            self.assertIn(s, page)
        page = c.get("/register").get_data(as_text=True)
        self.assertIn('autocomplete="tel-national"', page); self.assertIn('placeholder="03', page)
        # someone registered by USSD signs in once with the PIN, sets a password without a current one, then uses it
        with self.app.app_context():
            u = User(role="member", name="Pin Only", phone="+261349990011", language="en", pin_hash=generate_password_hash("4826"), is_active_flag=True)
            db.session.add(u); db.session.flush(); db.session.add(Household(user_id=u.id, name="Pin Only", village="Ampotaka")); db.session.commit()
        with self.app.test_request_context():
            change = url_for("change_password")
        c = self.app.test_client()
        r = post(c, "/login", {"identifier": "034 99 900 11", "cc": "MG", "password": "4826"})
        self.assertEqual(r.status_code, 302); self.assertTrue(r.headers["Location"].endswith(change))
        page = c.get(change).get_data(as_text=True)
        self.assertNotIn('name="current"', page); self.assertIn("Your USSD PIN stays the same.", page)
        self.assertEqual(post(c, change, {"password": "River Pump #2026", "confirm": "River Pump #2026"}).status_code, 302)
        login("+261349990011", "River Pump #2026")
        self.assertEqual(post(self.app.test_client(), "/login", {"identifier": "+261349990011", "password": "4826"}).status_code, 200)
        # an administrator: no USSD code in settings, an email (required) and no phone on the profile, users made in a dialog
        with self.app.app_context():
            db.session.add(User(role="admin", name="Round Admin", email="round-admin@cwas.test", password_hash=generate_password_hash("Round Admin #2026"), is_active_flag=True))
            m = User(role="member", name="Role Change", phone="+261349990022", password_hash=generate_password_hash("Role Change #2026"), is_active_flag=True)
            db.session.add(m); db.session.flush(); db.session.add(Household(user_id=m.id, name="Role Change", village="Ampotaka"))
            S.event(m, "Welcome {name}! Account created. Dial {dial} to book a slot.", "system", name="Role", dial=S.DIAL); db.session.commit(); mid = m.id
        ac = login("round-admin@cwas.test", "Round Admin #2026")
        page = ac.get("/admin/settings").get_data(as_text=True)
        self.assertNotIn("enroll_admin", page); self.assertIn("enroll_coord", page)
        with self.app.test_request_context():
            prof = url_for("profile")
        page = ac.get(prof).get_data(as_text=True)
        self.assertNotIn("Email (optional)", page); self.assertNotIn('value="" disabled>', page)
        page = ac.get("/admin/users").get_data(as_text=True)
        for s in ('popovertarget="user-new"', 'id="user-new" popover', "data-village", "user-form"):
            self.assertIn(s, page)
        # a role change clears the member welcome and says what the account is now
        self.assertEqual(post(ac, f"/admin/users/{mid}/role", {"role": "coordinator"}).status_code, 302)
        with self.app.app_context():
            keys = [n.key for n in Notification.query.filter_by(user_id=mid)]
        self.assertFalse(any(k.startswith("Welcome {name}! Account created.") for k in keys))
        self.assertIn("Your account is now a coordinator account.", keys)
        # links sent by SMS or email are https in production; a session lasts 30 days of use
        with self.app.test_request_context("/", base_url="http://cwas.example.org"):
            prod = self.app.config["IS_PROD"]; self.app.config["IS_PROD"] = True
            try:
                self.assertTrue(external_url("reset_password", token="abc").startswith("https://cwas.example.org/"))
            finally:
                self.app.config["IS_PROD"] = prod
        self.assertEqual(self.app.config["PERMANENT_SESSION_LIFETIME"].days, 30)
        # the four figures count up, active states are green in Default, cards are glass, the footer languages share a line
        self.assertEqual(self.app.test_client().get("/").get_data(as_text=True).count("data-count>"), 4)
        self.assertIn('e.hasAttribute("data-count")', read("static/js/fx.js"))
        self.assertIn("resolvedOptions().timeZone", read("static/js/sim.js"))
        css = read("static/css/input.css")
        for s in ("--active:0 126 58", ".field:focus{outline:none;border-color:rgb(var(--active,var(--hot)))", "polish 11: pure liquid glass",
                  ".user-form:has(select[name=role] option[value=member]:checked) [data-village]{display:block}", ".foot-bar :has(> .foot-lang){flex-wrap:nowrap"):
            self.assertIn(s, css)
        self.assertIn(".cc-panel", read("static/css/app.css"))
        # the deck is drawn at full size, the Default hero drop is red, the README walks through every setup step
        self.assertIn(".dk-card{--dk:3;", css); self.assertIn('html[data-theme="saina"] .tframe{background:#FC3D32}', css)
        readme = read("README.md")
        for s in ("git clone https://github.com/banituze/cwas-platform.git", "python3 -m venv .venv", ".venv\\Scripts\\Activate.ps1", "python3 -m unittest tests.test_platform", "${{Postgres.DATABASE_URL}}"):
            self.assertIn(s, readme)

    def test_104_install_card_theme_icons_and_lab_typing(self):
        root = os.path.dirname(os.path.abspath(A.__file__))
        read = lambda p: open(os.path.join(root, p), encoding="utf-8").read()
        c = self.app.test_client()
        page = c.get("/").get_data(as_text=True)
        for s in ("data-install", "js/install.js", "/manifest.webmanifest?theme=saina", 'data-brand="manifest"'):
            self.assertIn(s, page)
        for th, colour in (("saina", "#FFFFFF"), ("fotsy", "#FFFFFF"), ("maitso", "#007E3A"), ("mena", "#D42A20")):
            m = c.get(f"/manifest.webmanifest?theme={th}").get_json()
            self.assertEqual((m["id"], m["theme_color"]), ("/app", colour))
            for icon in m["icons"]:
                self.assertTrue(icon["src"].startswith(f"/static/icons/{th}/"))
                self.assertTrue(os.path.exists(os.path.join(root, icon["src"].lstrip("/"))))
            self.assertTrue(os.path.exists(os.path.join(root, "static", "icons", th, "apple-touch-icon.png")))
        self.assertTrue(c.get("/manifest.webmanifest?theme=nope").get_json()["icons"][0]["src"].startswith("/static/icons/saina/"))
        self.assertIn('set("manifest", `/manifest.webmanifest?theme=${th}`)', read("static/js/app.js"))
        js = read("static/js/install.js")
        for s in ("beforeinstallprompt", "appinstalled", "cwas-install-dismissed", "display-mode: standalone", "cwas:theme"):
            self.assertIn(s, js)
        css = read("static/css/input.css")
        for s in (".install-card{position:fixed", "@media print{.install-card{display:none!important}}", ".lab{touch-action:manipulation}",
                  ".lab :is(.stage,.os-view) :is(input,textarea,select){font-size:max(16px,1em)}", ".stage.is-typing{position:fixed"):
            self.assertIn(s, css)
        sim = read("static/js/sim.js")
        self.assertIn('stage.classList.add("is-typing")', sim); self.assertIn("visualViewport", sim)

if __name__ == "__main__":
    unittest.main()
