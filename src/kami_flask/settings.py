"""Typed settings read from the environment, shared by every Kamigiri app.

Apps subclass :class:`Settings` (a dataclass) and add their own fields with :func:`env`.
Values are read when the settings object is created, so tests can set ``os.environ``
first and then build the app.
"""

import os
from dataclasses import dataclass, field
from typing import Any

DEFAULT_SECRET_KEY = "dev-secret-key-change-in-production"
MIN_SECRET_LENGTH = 16
WEAK_PASSWORDS = {"", "admin", "password", "change-me"}

_TRUE = {"1", "true", "yes", "on"}


def _cast(name: str, raw: str, default: Any) -> Any:
    if isinstance(default, bool):
        return raw.strip().lower() in _TRUE
    try:
        if isinstance(default, int):
            return int(raw) if raw.strip() else default
        if isinstance(default, float):
            return float(raw) if raw.strip() else default
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None
    return raw


def env(name: str, default: Any) -> Any:
    """A dataclass field read from the environment variable ``name``.

    The type of ``default`` decides the conversion (bool, int, float, otherwise str).
    """
    return field(default_factory=lambda: _cast(name, os.environ[name], default) if name in os.environ else default)


@dataclass
class Settings:
    app_env: str = env("APP_ENV", "development")
    secret_key: str = env("SECRET_KEY", DEFAULT_SECRET_KEY)

    # Single-user auth. The dev defaults admin/admin are refused in production.
    auth_username: str = env("AUTH_USERNAME", "admin")
    auth_password: str = env("AUTH_PASSWORD", "admin")
    api_token: str = env("API_TOKEN", "")  # optional Bearer token for scripts and apps

    # Gateway auth: a reverse proxy (Authelia behind nginx) sends Remote-User plus this shared
    # secret in X-Kami-Gateway. Empty secret = gateway auth off, Remote-User is ignored.
    gateway_secret: str = env("GATEWAY_SECRET", "")
    gateway_logout_url: str = env("GATEWAY_LOGOUT_URL", "")  # where the SPA sends "log out"

    session_cookie_secure: bool = env("SESSION_COOKIE_SECURE", False)
    session_cookie_name: str = env("SESSION_COOKIE_NAME", "")  # default: <app>_session
    session_days: int = env("SESSION_DAYS", 14)

    # Reverse proxies in front of the app (1 behind the platform nginx).
    trusted_proxies: int = env("TRUSTED_PROXIES", 0)
    web_dist: str = env("WEB_DIST", "")

    log_format: str = env("LOG_FORMAT", "")  # json | text; default json in production
    log_level: str = env("LOG_LEVEL", "INFO")
    metrics_port: int = env("METRICS_PORT", 0)  # 0 = no metrics server

    # Baked into the image by CI (see /api/version).
    revision: str = env("APP_REVISION", "dev")
    built_at: str = env("APP_BUILT_AT", "dev")

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    def unsafe_settings(self) -> list[str]:
        """Names of settings that still hold a default or placeholder value (never the values)."""
        problems = []
        key = self.secret_key
        if key == DEFAULT_SECRET_KEY or key.startswith(("change-me", "dev-secret")) or len(key) < MIN_SECRET_LENGTH:
            problems.append("SECRET_KEY")
        if self.auth_password in WEAK_PASSWORDS:
            problems.append("AUTH_PASSWORD")
        # Empty = Bearer auth off. A set token grants full API access, so it must be long.
        if self.api_token and (self.api_token.startswith("change-me") or len(self.api_token) < MIN_SECRET_LENGTH):
            problems.append("API_TOKEN")
        # Empty = gateway auth off. A set secret lets the proxy vouch for any user, so it must be long.
        if self.gateway_secret and (
            self.gateway_secret.startswith("change-me") or len(self.gateway_secret) < MIN_SECRET_LENGTH
        ):
            problems.append("GATEWAY_SECRET")
        return problems

    def flask_config(self) -> dict[str, Any]:
        """App-specific Flask config keys. Override in subclasses."""
        return {}
