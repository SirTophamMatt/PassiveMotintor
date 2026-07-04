"""Admin authentication for the web deployment.

The dashboards (Overview / Flood / Power) are public and read-only. Everything
that changes state — Start/Stop collection, Settings, Import, tag management,
data export — is gated behind an admin session.

Auth model
----------
- The admin password comes from the ``UM_ADMIN_PASSWORD`` environment variable
  (preferred for a server deployment) or, failing that, a hash saved in
  ``config.json`` under ``web.admin_password_hash`` (set from the Admin page).
- A successful login sets ``session['is_admin']`` in a Flask cookie signed with
  the app's secret key (``UM_SECRET_KEY``).
- The **desktop** build is single-user on localhost, so it is treated as admin
  automatically — no login prompt there.
"""
import hmac
import logging
import os

import flask
from werkzeug.security import check_password_hash, generate_password_hash

from app.config import load_config, save_config

log = logging.getLogger(__name__)

SESSION_KEY = "is_admin"


def _desktop():
    return os.environ.get("UM_DESKTOP") == "1"


def admin_password_configured():
    """True if some admin password (env or config hash) is set."""
    if os.environ.get("UM_ADMIN_PASSWORD"):
        return True
    return bool(load_config().get("web", {}).get("admin_password_hash"))


def verify_password(password):
    """Constant-time check of a submitted password against env or config hash."""
    if not password:
        return False
    env_pw = os.environ.get("UM_ADMIN_PASSWORD")
    if env_pw:
        return hmac.compare_digest(str(password), str(env_pw))
    stored = load_config().get("web", {}).get("admin_password_hash")
    if stored:
        try:
            return check_password_hash(stored, password)
        except Exception:
            return False
    return False


def set_admin_password(password):
    """Persist a new admin password hash to config.json (Admin page use)."""
    cfg = load_config()
    cfg.setdefault("web", {})["admin_password_hash"] = generate_password_hash(password)
    save_config(cfg)


def login():
    flask.session[SESSION_KEY] = True
    flask.session.permanent = True


def logout():
    flask.session.pop(SESSION_KEY, None)


def is_admin():
    """Whether the current request is an authenticated admin.

    Returns True on the desktop build (implicit trust) and False when called
    outside a request context (e.g. at import time)."""
    if _desktop():
        return True
    try:
        return bool(flask.session.get(SESSION_KEY))
    except RuntimeError:
        return False
