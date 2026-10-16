"""KamiApp: a Flask app with the Kamigiri platform contract already wired in."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from . import auth, errors, health, logs, metrics, spa
from .settings import Settings


class KamiApp:
    """Build the Flask app for ``name`` (e.g. "tasks").

    Hook order matters: request logging and metrics run first, then the auth guard, then
    whatever ``before_request`` hooks the app registers after construction (e.g. opening a
    DB session), so an unauthenticated request never touches the database.
    """

    def __init__(
        self,
        name: str,
        settings: Settings,
        *,
        version: str = "0.0.0",
        import_name: str | None = None,
        ready_check: Callable[[], Any] | None = None,
    ) -> None:
        if settings.is_production:
            unsafe = settings.unsafe_settings()
            if unsafe:
                raise RuntimeError(
                    "Refusing to start in production with default or placeholder values for: "
                    + ", ".join(unsafe)
                    + ". Set them in .env (see .env.example)."
                )
        self.name = name
        self.settings = settings
        self.version = version
        self.ready_check = ready_check
        self.error_hooks: list[Callable[[Exception], Any]] = []
        self.throttle = auth.LoginThrottle()

        fmt = settings.log_format or ("json" if settings.is_production else "text")
        logs.setup_logging(name, fmt, settings.log_level)

        app = Flask(import_name or name)
        app.config.update(self._flask_config())
        app.config.update(settings.flask_config())
        app.extensions["kami"] = self
        app.extensions["settings"] = settings

        logs.init(app, self)
        metrics.init(app, self)
        auth.init(app)
        health.init(app, self)
        errors.init(app, self)

        if settings.trusted_proxies > 0:
            n = settings.trusted_proxies
            app.wsgi_app = ProxyFix(app.wsgi_app, x_for=n, x_proto=n)  # type: ignore[method-assign]
        self.flask = app

    def _flask_config(self) -> dict[str, Any]:
        s = self.settings
        return {
            "SECRET_KEY": s.secret_key,
            "SESSION_COOKIE_NAME": s.session_cookie_name or f"{self.name}_session",
            "SESSION_COOKIE_HTTPONLY": True,
            "SESSION_COOKIE_SAMESITE": "Lax",
            "SESSION_COOKIE_SECURE": s.session_cookie_secure,
            # Apps are told apart by cookie name; a path would break `ng serve` (/api, not /<app>/api).
            "SESSION_COOKIE_PATH": "/",
            "SESSION_REFRESH_EACH_REQUEST": True,
            "PERMANENT_SESSION_LIFETIME": timedelta(days=s.session_days),
        }

    def on_error(self, hook: Callable[[Exception], Any]) -> Callable[[Exception], Any]:
        """Run ``hook(error)`` for unhandled exceptions (e.g. roll back the DB session)."""
        self.error_hooks.append(hook)
        return hook

    def serve_spa(self, dist: str | Path | None = None) -> None:
        """Serve the built SPA. Call it last, after the app registered its routes."""
        spa.init(self.flask, dist if dist is not None else self.settings.web_dist)
