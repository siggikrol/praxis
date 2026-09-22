# auth.py
import os, json, time
import msal
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, current_app
from werkzeug.security import check_password_hash, generate_password_hash

bp = Blueprint("auth", __name__, url_prefix="/auth")

def _load_users() -> dict:
    """
    Users come from:
      A) AUTH_USERS_JSON='{"alice":"<pbkdf2 hash>", "bob":"<hash>"}'
      B) BASIC_USER + BASIC_PASSWORD (plaintext -> hashed once)
    """
    raw = os.environ.get("AUTH_USERS_JSON")
    if raw:
        try:
            data = json.loads(raw)
            return {str(k): str(v) for k, v in data.items() if k and v}
        except Exception:
            pass
    u = os.environ.get("BASIC_USER")
    p = os.environ.get("BASIC_PASSWORD")
    if u and p:
        return {u: generate_password_hash(p)}
    return {}

@bp.route("/login", methods=["GET", "POST"])
def login():
    users = _load_users()
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if username in users and check_password_hash(users[username], password):
            session["user"] = username
            session["auth_method"] = "basic"
            session["login_ts"] = int(time.time())
            try:
                from services.session_activity import touch_active_session

                touch_active_session(
                    session_dir=current_app.config.get("SESSION_FILE_DIR", "/tmp/flask_sessions"),
                    sid=getattr(session, "sid", None),
                    user=username,
                    auth_method="basic",
                    login_ts=session.get("login_ts"),
                    force=True,
                )
            except Exception:
                pass
            return redirect(url_for("index"))
        flash("Invalid username or password", "danger")
    # Render with original next (if any); template will omit hidden field when empty
    return render_template("login.html", next=request.args.get("next"))



@bp.route("/logout")
def logout():
    user = session.get("user")
    try:
        from services.session_activity import remove_active_session

        remove_active_session(
            session_dir=current_app.config.get("SESSION_FILE_DIR", "/tmp/flask_sessions"),
            sid=getattr(session, "sid", None),
            user=user,
        )
    except Exception:
        pass
    session.pop("user", None)
    session.pop("auth_method", None)
    session.pop("login_ts", None)
    return redirect(url_for("auth.login"))

# ---------------- Azure AD Login -----------------

@bp.route("/aad_login")
def aad_login():
    client = msal.ConfidentialClientApplication(
        os.getenv("CLIENT_ID"),
        authority=f"https://login.microsoftonline.com/{os.getenv('TENANT_ID')}",
        client_credential=os.getenv("CLIENT_SECRET"),
    )

    auth_url = client.get_authorization_request_url(
        scopes=["User.Read"],
        redirect_uri=url_for("auth.aad_callback", _external=True),
    )
    return redirect(auth_url)


@bp.route("/aad_callback")
def aad_callback():
    code = request.args.get("code")
    if not code:
        return redirect(url_for("auth.login"))

    client = msal.ConfidentialClientApplication(
        os.getenv("CLIENT_ID"),
        authority=f"https://login.microsoftonline.com/{os.getenv('TENANT_ID')}",
        client_credential=os.getenv("CLIENT_SECRET"),
    )

    result = client.acquire_token_by_authorization_code(
        code,
        scopes=["User.Read"],
        redirect_uri=url_for("auth.aad_callback", _external=True),
    )

    if "access_token" in result:
        user = result.get("id_token_claims", {}).get("preferred_username", "aad_user")
        session["user"] = user
        session["auth_method"] = "aad"
        session["login_ts"] = int(time.time())
        try:
            from services.session_activity import touch_active_session

            touch_active_session(
                session_dir=current_app.config.get("SESSION_FILE_DIR", "/tmp/flask_sessions"),
                sid=getattr(session, "sid", None),
                user=user,
                auth_method="aad",
                login_ts=session.get("login_ts"),
                force=True,
            )
        except Exception:
            pass
        return redirect(url_for("index"))
    else:
        return f"Azure AD Login failed: {result.get('error_description', 'Unknown error')}"
