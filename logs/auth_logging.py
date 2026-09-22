import logging
from flask_login import user_logged_in, user_logged_out, user_login_failed

log = logging.getLogger("app.auth")

def _user_id(u):
    # Attempts common attributes without importing models
    for attr in ("id", "sub", "user_id", "username", "email"):
        if hasattr(u, attr):
            return getattr(u, attr)
    return str(u)

def init_auth_logging(app):
    try:
        @user_logged_in.connect_via(app)
        def _on_login(sender, user):
            log.info("login_success", extra={"user_id": _user_id(user)})

        @user_logged_out.connect_via(app)
        def _on_logout(sender, user):
            log.info("logout", extra={"user_id": _user_id(user)})

        @user_login_failed.connect_via(app)
        def _on_login_failed(sender, user):
            # 'user' may be a dict or identifier depending on the app’s login logic
            log.warning("login_failed", extra={"user_id": _user_id(user)})
    except Exception:
        # Skip if Flask-Login not installed
        log.debug("flask_login_signals_unavailable")
