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


class Notification(db.Model):
    __tablename__ = "notifications"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = db.Column(db.String(16), nullable=False, default="info")  # booking|wallet|maintenance|announcement|system
    key = db.Column(db.Text, nullable=False, default="")  # English source sentence (translation key); empty for free text
    params = db.Column(db.Text, nullable=False, default="{}")        # JSON parameters for the key
    body = db.Column(db.Text, nullable=False, default="")            # free text (announcements)
    is_read = db.Column(db.Boolean, nullable=False, default=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class Maintenance(db.Model):
    __tablename__ = "maintenance"
    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, db.ForeignKey("water_sources.id"), nullable=False, index=True)
    starts_at = db.Column(db.DateTime, nullable=False)  # local wall-clock time
    ends_at = db.Column(db.DateTime, nullable=False)
    reason = db.Column(db.String(255), nullable=False, default="")
    status = db.Column(db.String(12), nullable=False, default="scheduled")  # scheduled|cancelled
    created_by = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    source = db.relationship("WaterSource")


class AuditLog(db.Model):
    """Append-only, hash-chained. Each row commits to the one before it, so edits are detectable."""
    __tablename__ = "audit_logs"
    id = db.Column(db.Integer, primary_key=True)
    at = db.Column(db.DateTime, nullable=False, default=utcnow)
    actor_id = db.Column(db.Integer, nullable=True)
    actor_label = db.Column(db.String(190), nullable=False, default="system")
    channel = db.Column(db.String(12), nullable=False, default="web")
    action = db.Column(db.String(48), nullable=False, index=True)
    entity = db.Column(db.String(32), nullable=False, default="")
    entity_id = db.Column(db.String(32), nullable=False, default="")
    detail = db.Column(db.String(500), nullable=False, default="")
    ip = db.Column(db.String(64), nullable=False, default="")
    prev_hash = db.Column(db.String(64), nullable=False, default="")
    hash = db.Column(db.String(64), nullable=False, default="")


class Setting(db.Model):
    __tablename__ = "settings"
    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.String(255), nullable=False, default="")


class UssdSession(db.Model):
    __tablename__ = "ussd_sessions"
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(80), unique=True, nullable=False)
    phone = db.Column(db.String(24), nullable=False, index=True)
    channel = db.Column(db.String(12), nullable=False, default="telco")  # telco|simulator
    trail = db.Column(db.String(400), nullable=False, default="")  # masked input trail, never PINs
    last_response = db.Column(db.String(200), nullable=False, default="")
    hops = db.Column(db.Integer, nullable=False, default=0)
    ended = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class SmsLog(db.Model):
    __tablename__ = "sms_logs"
    id = db.Column(db.Integer, primary_key=True)
    direction = db.Column(db.String(3), nullable=False)  # in|out
    phone = db.Column(db.String(24), nullable=False, index=True)
    body = db.Column(db.String(700), nullable=False)
    status = db.Column(db.String(16), nullable=False, default="queued")  # queued|sent|simulated|failed|received
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class PasswordReset(db.Model):
    __tablename__ = "password_resets"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash = db.Column(db.String(64), unique=True, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class ChatThread(db.Model):
    __tablename__ = "chat_threads"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = db.Column(db.String(80), nullable=False, default="New chat")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    messages = db.relationship("ChatMessage", backref="thread", cascade="all, delete-orphan", order_by="ChatMessage.id")


class ChatMessage(db.Model):
    __tablename__ = "chat_messages"
    id = db.Column(db.Integer, primary_key=True)
    thread_id = db.Column(db.Integer, db.ForeignKey("chat_threads.id", ondelete="CASCADE"), nullable=False, index=True)
    role = db.Column(db.String(9), nullable=False)  # user|assistant
    body = db.Column(db.Text, nullable=False, default="")
    files = db.Column(db.Text, nullable=False, default="[]")  # JSON: [{fid, name, mime, kind, size}]
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class PilotFollower(db.Model):
    """An email address that asked for a short note when the pilot reaches a milestone. Nothing else is stored."""
    __tablename__ = "pilot_followers"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(190), unique=True, nullable=False)
    language = db.Column(db.String(2), nullable=False, default="en")
    token = db.Column(db.String(40), unique=True, nullable=False)  # one-click leave link
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
