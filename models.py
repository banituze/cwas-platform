"""CWAS data model. One SQLAlchemy schema that runs on SQLite (default) and PostgreSQL."""
from datetime import datetime, timezone

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Index, text

db = SQLAlchemy()


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    role = db.Column(db.String(16), nullable=False, default="member", index=True)  # member|coordinator|admin
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(190), unique=True, nullable=True)
    phone = db.Column(db.String(24), unique=True, nullable=True)
    password_hash = db.Column(db.String(255), nullable=True)
    pin_hash = db.Column(db.String(255), nullable=True)
    language = db.Column(db.String(2), nullable=False, default="mg")
    is_active_flag = db.Column("is_active", db.Boolean, nullable=False, default=True)
    must_change_password = db.Column(db.Boolean, nullable=False, default=False)
    mfa_secret = db.Column(db.String(64), nullable=True)
    mfa_enabled = db.Column(db.Boolean, nullable=False, default=False)
    failed_logins = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime, nullable=True)
    pin_failed = db.Column(db.Integer, nullable=False, default=0)
    pin_locked_until = db.Column(db.DateTime, nullable=True)
    recovery_hash = db.Column(db.String(255), nullable=True)  # 6-digit code chosen at registration; resets a forgotten PIN
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_login_at = db.Column(db.DateTime, nullable=True)
    household = db.relationship("Household", back_populates="user", uselist=False, cascade="all, delete-orphan")

    @property
    def is_active(self):  # Flask-Login hook
        return bool(self.is_active_flag)

    @property
    def is_staff(self):
        return self.role in ("coordinator", "admin")


class Household(db.Model):
    __tablename__ = "households"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    village = db.Column(db.String(120), nullable=False, default="")
    address = db.Column(db.String(255), nullable=False, default="")
    family_size = db.Column(db.Integer, nullable=False, default=4)
    priority_level = db.Column(db.String(12), nullable=False, default="standard")  # standard|elevated|high
    distance_m = db.Column(db.Integer, nullable=False, default=500)
    access_needs = db.Column(db.String(255), nullable=False, default="")
    vuln_flags = db.Column(db.String(80), nullable=False, default="")  # self-reported needs, comma codes (services.VULN)
    needs_review = db.Column(db.Boolean, nullable=False, default=False)  # a self-report waits for a coordinator's check
    home_source_id = db.Column(db.Integer, db.ForeignKey("water_sources.id", ondelete="SET NULL"), nullable=True)
    home_source_note = db.Column(db.String(80), nullable=False, default="")  # a water point not in the list, named by the household
    balance = db.Column(db.Integer, nullable=False, default=0)  # Ariary, cached from posted ledger rows
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    user = db.relationship("User", back_populates="household")
    home_source = db.relationship("WaterSource", foreign_keys=[home_source_id])
    bookings = db.relationship("Booking", back_populates="household", order_by="Booking.id.desc()")


class WaterSource(db.Model):
    __tablename__ = "water_sources"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    kind = db.Column(db.String(16), nullable=False, default="borehole")  # borehole|well|tap
    village = db.Column(db.String(120), nullable=False, default="")
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    status = db.Column(db.String(16), nullable=False, default="operational", index=True)  # operational|maintenance|closed
    open_min = db.Column(db.Integer, nullable=False, default=360)   # minutes from midnight
    close_min = db.Column(db.Integer, nullable=False, default=1080)
    slot_minutes = db.Column(db.Integer, nullable=False, default=30)
    slot_capacity = db.Column(db.Integer, nullable=False, default=6)     # bookings per slot
    daily_capacity = db.Column(db.Integer, nullable=False, default=80)   # bookings per day
    tariff_per_100l = db.Column(db.Integer, nullable=False, default=150)  # Ariary per 100 L
    max_litres = db.Column(db.Integer, nullable=False, default=100)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class Booking(db.Model):
    __tablename__ = "bookings"
    id = db.Column(db.Integer, primary_key=True)
    ref = db.Column(db.String(24), unique=True, nullable=False)
    household_id = db.Column(db.Integer, db.ForeignKey("households.id"), nullable=False, index=True)
    source_id = db.Column(db.Integer, db.ForeignKey("water_sources.id"), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False, index=True)
    start_min = db.Column(db.Integer, nullable=False)
    end_min = db.Column(db.Integer, nullable=False)
    litres = db.Column(db.Integer, nullable=False)
    amount = db.Column(db.Integer, nullable=False, default=0)
    discount_pct = db.Column(db.Integer, nullable=False, default=0)
    # pending|approved|denied|cancelled|collected|no_show   (SRS booking lifecycle)
    status = db.Column(db.String(12), nullable=False, default="pending", index=True)
    channel = db.Column(db.String(12), nullable=False, default="web")  # web|ussd|system
    priority_score = db.Column(db.Integer, nullable=False, default=0)
    ai_note = db.Column(db.String(255), nullable=False, default="")
    ai_suggestion = db.Column(db.String(8), nullable=False, default="review")  # approve|review|deny
    decision_note = db.Column(db.String(255), nullable=False, default="")
    decided_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    decided_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    household = db.relationship("Household", back_populates="bookings")
    source = db.relationship("WaterSource")

    __table_args__ = (
        Index("ux_one_booking_per_household_day", "household_id", "date", unique=True,
              sqlite_where=text("status IN ('pending','approved','collected')"),
              postgresql_where=text("status IN ('pending','approved','collected')")),
        Index("ix_booking_slot", "source_id", "date", "start_min"),
    )

    ACTIVE = ("pending", "approved", "collected")


class WalletTxn(db.Model):
    """Append-only wallet ledger. `amount` is signed: deposits and refunds are positive, debits negative."""
    __tablename__ = "wallet_txns"
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.Integer, db.ForeignKey("households.id"), nullable=False, index=True)
    kind = db.Column(db.String(16), nullable=False)  # deposit|booking_debit|booking_refund|adjustment
    amount = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(10), nullable=False, default="posted")  # posted|pending|failed
    provider = db.Column(db.String(16), nullable=False, default="")  # orange|airtel|cash|wallet
    reference = db.Column(db.String(32), unique=True, nullable=False)
    booking_id = db.Column(db.Integer, db.ForeignKey("bookings.id"), nullable=True, index=True)
    balance_after = db.Column(db.Integer, nullable=True)
    note = db.Column(db.String(255), nullable=False, default="")
    created_by = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    household = db.relationship("Household")
    booking = db.relationship("Booking")

    __table_args__ = (
        # A wallet-funded booking receives at most one linked refund and one debit.
        Index("ux_one_debit_per_booking", "booking_id", "kind", unique=True,
              sqlite_where=text("kind IN ('booking_debit','booking_refund') AND status='posted'"),
              postgresql_where=text("kind IN ('booking_debit','booking_refund') AND status='posted'")),
    )


