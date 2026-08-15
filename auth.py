"""Authentication.

Two different callers with two different needs:

*Coordinators* open the dashboard in a browser. They get a server-side session
after a login form. The previous HTTP Basic scheme was replaced because the
browser replays Basic credentials on every request to the realm automatically,
which — combined with the ``@csrf.exempt`` decorators that were on every
mutating route — meant any page a logged-in coordinator visited could delete
or rewrite fixes.

*Field devices* upload readings from a phone that may be offline for hours.
They send a shared token in a header. This is not per-user auth, and is not
trying to be: it stops anonymous writes from corrupting the dataset, which is
the actual risk. Per-device credentials are the next step.
"""
import hmac
from functools import wraps

from flask import current_app, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash

SESSION_KEY = "coordinator"
FIELD_TOKEN_HEADER = "X-Field-Token"


def verify_coordinator(username: str, password: str) -> bool:
    config = current_app.config["PANGOT"]
    # compare_digest on the username too, so a wrong username and a wrong
    # password take the same time to reject.
    username_ok = hmac.compare_digest((username or "").strip(), config.admin_username)
    password_ok = check_password_hash(config.admin_password_hash, password or "")
    return username_ok and password_ok


def log_in(username: str) -> None:
    session.clear()
    session[SESSION_KEY] = username
    session.permanent = True


def log_out() -> None:
    session.clear()


def current_coordinator():
    return session.get(SESSION_KEY)


def requires_coordinator(view):
    """Gate a browser page or JSON API behind the coordinator session."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_coordinator():
            if request.accept_mimetypes.best == "application/json" or request.path.startswith("/api/"):
                return jsonify({"status": "error", "message": "Sign in required"}), 401
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapper


def requires_field_token(view):
    """Gate an upload endpoint behind the shared field-device token."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        config = current_app.config["PANGOT"]
        supplied = request.headers.get(FIELD_TOKEN_HEADER, "")
        # A coordinator session is also sufficient — it is strictly stronger,
        # and it keeps the dashboard able to call these endpoints.
        if current_coordinator() or hmac.compare_digest(supplied, config.field_token):
            return view(*args, **kwargs)
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "This device is not paired. Enter the field token in Settings.",
                    "code": "unpaired",
                }
            ),
            401,
        )

    return wrapper
