"""Shared Flask extension objects. Kept apart from app.py so that running `python3 app.py` never imports the app twice."""
import threading

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address, default_limits=["600 per hour"], storage_uri="memory://")
login_manager = LoginManager()
WRITE_LOCK = threading.RLock()  # one writer at a time keeps SQLite, the audit chain and slot counts consistent
