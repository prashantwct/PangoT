"""Authentication.

Two different callers with two different needs:

*Coordinators* open the dashboard in a browser. They sign in with their own
account and get a server-side session. The previous HTTP Basic scheme was
replaced because the browser replays Basic credentials on every request to the
realm automatically, which — combined with the ``@csrf.exempt`` decorators that
were on every mutating route — meant any page a logged-in coordinator visited
could delete or rewrite fixes. Per-user accounts also make deletions
attributable, which a single shared password never could.

*Field devices* upload readings from a phone that may be offline for hours.
They send a shared token in a header. This is not per-user auth and is not
trying to be: it stops anonymous writes from corrupting the dataset, which is
the actual risk. Per-device credentials are the next step.
"""
import hmac
from functools import wraps

from flask import current_app, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from extensions import db
from models import User, utcnow

SESSION_KEY = "coordinator"
SESSION_ROLE = "coordinator_role"
FIELD_TOKEN_HEADER = "X-Field-Token"

ROLES = ("admin", "coordinator")


def create_user(username: str, password: str, role: str = "coordinator") -> User:
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    user = User(
        username=username.strip(),
        password_hash=generate_password_hash(password),
        role=role,
    )
    db.session.add(user)
    db.session.commit()
    return user


def set_password(user: User, password: str) -> None:
    user.password_hash = generate_password_hash(password)
    db.session.commit()


def authenticate(username: str, password: str):
    """Return the signed-in identity, or None.

    Falls back to the environment's ``ADMIN_USERNAME`` while no accounts exist,
    so a fresh deployment is reachable before anyone has run ``flask users
    create``. Once a single account exists the fallback stops applying — leaving
    it live would be a permanent second way in.
    """
    username = (username or "").strip()
    config = current_app.config["PANGOT"]

    user = User.query.filter_by(username=username).first()
    if user:
        if not user.is_active:
            return None
        if not check_password_hash(user.password_hash, password or ""):
            return None
        user.last_login_at = utcnow()
        db.session.commit()
        return {"username": user.username, "role": user.role}

    if User.query.count() == 0:
        # compare_digest on the username too, so a wrong username and a wrong
        # password take the same time to reject.
        username_ok = hmac.compare_digest(username, config.admin_username)
        password_ok = check_password_hash(config.admin_password_hash, password or "")
        if username_ok and password_ok:
            return {"username": config.admin_username, "role": "admin"}

    return None


def log_in(identity) -> None:
    session.clear()
    session[SESSION_KEY] = identity["username"]
    session[SESSION_ROLE] = identity["role"]
    session.permanent = True


def log_out() -> None:
    session.clear()


def current_coordinator():
    return session.get(SESSION_KEY)


def current_role():
    return session.get(SESSION_ROLE, "coordinator")


def _unauthenticated():
    if request.accept_mimetypes.best == "application/json" or request.path.startswith("/api/"):
        return jsonify({"status": "error", "message": "Sign in required"}), 401
    return redirect(url_for("login", next=request.path))


def requires_coordinator(view):
    """Gate a browser page or JSON API behind a signed-in coordinator."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_coordinator():
            return _unauthenticated()
        return view(*args, **kwargs)

    return wrapper


def requires_admin(view):
    """Gate account management behind an admin account."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_coordinator():
            return _unauthenticated()
        if current_role() != "admin":
            if request.path.startswith("/api/"):
                return jsonify({"status": "error", "message": "Admin access required"}), 403
            return jsonify({"status": "error", "message": "Admin access required"}), 403
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
