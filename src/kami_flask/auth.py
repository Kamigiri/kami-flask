"""Single-user authentication: session cookie for the web app, optional Bearer token.

Every /api endpoint is protected; only the paths in ``OPEN_PATHS`` stay open. Cookie-authed
mutations must send ``X-Requested-With: XMLHttpRequest`` (CSRF hardening: a cross-site
form cannot set that header). Static SPA files are not under /api and stay public, so the
login screen can load.
"""

from __future__ import annotations

import hmac
import threading
import time
from collections import defaultdict
from typing import TYPE_CHECKING

from flask import Blueprint, Flask, current_app, g, jsonify, request, session

if TYPE_CHECKING:
    from .app import KamiApp

OPEN_PATHS = {"/api/health", "/api/ready", "/api/version", "/api/auth/login"}
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
THROTTLE_WINDOW_SEC = 300
THROTTLE_MAX_FAILURES = 5

bp = Blueprint("kami_auth", __name__, url_prefix="/api/auth")


def same(given: str, expected: str) -> bool:
    """Constant-time comparison, so response timing reveals nothing about the secret."""
    return hmac.compare_digest(given.encode(), expected.encode())


def _kami() -> KamiApp:
    return current_app.extensions["kami"]


def gateway_user() -> str | None:
    """The user vouched for by the trusted gateway, or None.

    Needs a configured secret, a matching ``X-Kami-Gateway`` header (constant-time) and a
    non-empty ``Remote-User``. Without the secret the user header is ignored completely.
    """
    secret = _kami().settings.gateway_secret
    if not secret or not same(request.headers.get("X-Kami-Gateway", ""), secret):
        return None
    return request.headers.get("Remote-User", "").strip() or None


def bearer_ok() -> bool:
    token = _kami().settings.api_token
    if not token:
        return False
    return same(request.headers.get("Authorization", ""), f"Bearer {token}")


class LoginThrottle:
    """In-memory failures per client IP (one gunicorn worker per app, several threads)."""

    def __init__(self) -> None:
        self._failures: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def blocked(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            recent = [t for t in self._failures.get(ip, []) if now - t < THROTTLE_WINDOW_SEC]
            if recent:
                self._failures[ip] = recent
            else:
                self._failures.pop(ip, None)  # forget clients whose failures expired
            return len(recent) >= THROTTLE_MAX_FAILURES

    def fail(self, ip: str) -> None:
        with self._lock:
            self._failures[ip].append(time.time())

    def clear(self) -> None:
        with self._lock:
            self._failures.clear()


def init(app: Flask) -> None:
    @app.before_request
    def _require_auth():
        path = request.path
        if not path.startswith("/api") or path in OPEN_PATHS:
            return None

        gateway = gateway_user()
        bearer = not gateway and bearer_ok()
        user = session.get("auth_user")
        if gateway:
            g.kami_user, g.kami_auth = gateway, "gateway"
        elif bearer:
            g.kami_user, g.kami_auth = "token", "token"
        elif user:
            g.kami_user, g.kami_auth = user, "session"
        else:
            return jsonify({"error": "Not authenticated"}), 401
        # Gateway and session auth are cookie-based, so writes need the XHR header; Bearer is exempt.
        if (
            request.method in MUTATING_METHODS
            and not bearer
            and request.headers.get("X-Requested-With") != "XMLHttpRequest"
        ):
            return jsonify({"error": "Missing X-Requested-With header"}), 403
        return None

    app.register_blueprint(bp)


@bp.post("/login")
def login():
    kami = _kami()
    ip = request.remote_addr or "unknown"
    if kami.throttle.blocked(ip):
        return jsonify({"error": "Too many attempts, try again later"}), 429

    data = request.get_json(silent=True) or {}
    username = str(data.get("username", ""))
    password = str(data.get("password", ""))
    # Evaluate both comparisons so timing does not reveal which one failed.
    user_ok = same(username, kami.settings.auth_username)
    password_ok = same(password, kami.settings.auth_password)
    if user_ok and password_ok:
        session.clear()
        session.permanent = True
        session["auth_user"] = username
        return jsonify({"username": username})

    kami.throttle.fail(ip)
    return jsonify({"error": "Invalid credentials"}), 401


@bp.post("/logout")
def logout():
    session.clear()
    return jsonify({"status": "logged_out"})


@bp.get("/me")
def me():
    gateway = gateway_user()
    if gateway:
        return jsonify({"username": gateway, "auth": "gateway", "logout_url": _kami().settings.gateway_logout_url})
    if session.get("auth_user"):
        return jsonify({"username": session["auth_user"], "auth": "session"})
    if bearer_ok():
        return jsonify({"username": _kami().settings.auth_username, "auth": "token"})
    return jsonify({"error": "Not authenticated"}), 401
