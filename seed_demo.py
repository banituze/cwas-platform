"""Demo people and history so every screen, report and forecast has something to show.
Runs only when SEED_DEMO=1 (the default outside production). Names below are invented."""
import random
from datetime import timedelta

from werkzeug.security import generate_password_hash

import services as S
from models import Booking, Household, User, WaterSource, db, utcnow

DEMO_PASSWORD = "Demo Water @2026"
COORD = ("coordinator@cwas.demo", "+261340000001", "Naly Rasoanirina")
MEMBERS = [
    ("Rasoa Vololona", "Ampotaka Centre", 6, "high", 900, "Elderly member"),
    ("Rakoto Jean", "Ampotaka Centre", 5, "standard", 300, ""),
    ("Hanta Razafy", "Ampotaka Est", 7, "elevated", 1400, ""),
    ("Andry Rabe", "Ampotaka Ouest", 4, "standard", 800, ""),
    ("Fara Rasoanaivo", "Ampotaka Est", 3, "high", 1900, "Person with disability"),
    ("Tiana Randria", "Ampotaka Centre", 8, "elevated", 600, ""),
    ("Miora Razanadrakoto", "Ampotaka Ouest", 5, "standard", 1100, ""),
    ("Solo Andriamihaja", "Ampotaka Centre", 4, "standard", 450, ""),
]


def seed_demo():
    rnd = random.Random(2026)
    coord = User(role="coordinator", name=COORD[2], email=COORD[0], phone=COORD[1], language="fr", password_hash=generate_password_hash(DEMO_PASSWORD),
                 pin_hash=generate_password_hash("2468"))
    db.session.add(coord)
    households = []
    for i, (name, village, size, level, dist, needs) in enumerate(MEMBERS, 1):
        u = User(role="member", name=name, phone=f"+2613400001{i:02d}", language=("mg", "fr", "en")[i % 3], password_hash=generate_password_hash(DEMO_PASSWORD),
                 pin_hash=generate_password_hash("1234"))
        db.session.add(u)
        db.session.flush()
        h = Household(user_id=u.id, name=name, village=village, family_size=size, priority_level=level, distance_m=dist, access_needs=needs)
        db.session.add(h)
        db.session.flush()
        S.wallet_post(h, "deposit", 40000, rnd.choice(("orange", "airtel")), note="demo opening balance")
        households.append(h)
    sources = WaterSource.query.filter_by(status="operational").all()
    today = S.today_local()
    used = set()
    for back in range(28, 0, -1):
        day = today - timedelta(days=back)
        for h in households:
            if rnd.random() < 0.55 and (h.id, day) not in used:
                src = rnd.choice(sources)
                litres = rnd.choice((40, 60, 80, 100)) if h.family_size > 4 else rnd.choice((20, 40, 60))
                start = src.open_min + src.slot_minutes * rnd.choice((0, 1, 2, 4, 6, 8, 10, 14, 18))
                start = min(start, src.close_min - src.slot_minutes)
                amount, pct, _ = S.price_quote(h, src, litres)
                status = rnd.choices(("collected", "no_show", "cancelled", "denied"), (78, 8, 8, 6))[0]
                b = Booking(ref=S.new_ref("CW"), household_id=h.id, source_id=src.id, date=day, start_min=start, end_min=start + src.slot_minutes, litres=litres,
                            amount=amount, discount_pct=pct, status=status, channel=rnd.choice(("web", "ussd", "ussd")), priority_score=S.priority_breakdown(h)[0],
                            ai_note="Within normal pattern", created_at=utcnow() - timedelta(days=back, hours=2))
                db.session.add(b)
                db.session.flush()
                S.wallet_post(h, "booking_debit", -amount, "wallet", note=f"Booking {b.ref}", booking=b)
                if status in ("cancelled", "denied"):
                    S.wallet_post(h, "booking_refund", amount, "wallet", note=f"Refund {b.ref}", booking=b)
                else:
                    used.add((h.id, day))
                # keep history from draining the demo wallets
                if h.balance < 8000:
                    S.wallet_post(h, "deposit", 25000, "orange", note="demo top-up")
    db.session.commit()
    # live queue: a few pending requests for the next days
    for h, offs, hour in zip(households[:5], (0, 1, 1, 2, 3), (12, 8, 9, 7, 10)):
        src = sources[h.id % len(sources)]
        day = today + timedelta(days=offs)
        slots = [s for s in S.slot_list(src, day, only_open=True) if s["start_min"] >= hour * 60]
        if slots:
            try:
                S.create_booking(h, src.id, day, slots[0]["start_min"], 40 if h.family_size < 6 else 60, "ussd" if h.id % 2 else "web")
            except S.ServiceError:
                db.session.rollback()
    S.notify(households[0].user, "Welcome to CWAS. Dial *384*9411# to book water, add money and check your balance.", "system")
    S.audit("seed.demo", "system", "", "demo coordinator, households and history", channel="system")
    db.session.commit()
