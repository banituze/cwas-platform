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


