import pytest

from kami_flask import KamiApp, Settings


def test_env_values_are_read_and_cast(monkeypatch):
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "true")
    monkeypatch.setenv("TRUSTED_PROXIES", "1")
    monkeypatch.setenv("AUTH_USERNAME", "me")
    s = Settings()
    assert s.session_cookie_secure is True
    assert s.trusted_proxies == 1
    assert s.auth_username == "me"


def test_defaults_are_flagged(monkeypatch):
    for name in ("SECRET_KEY", "AUTH_PASSWORD", "API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert Settings().unsafe_settings() == ["SECRET_KEY", "AUTH_PASSWORD"]
    weak = Settings(secret_key="change-me-please-long", auth_password="pw", api_token="change-me")
    assert weak.unsafe_settings() == ["SECRET_KEY", "API_TOKEN"]


def test_short_api_token_is_unsafe():
    s = Settings(secret_key="x" * 32, auth_password="real-pw-123", api_token="short")
    assert s.unsafe_settings() == ["API_TOKEN"]
    assert Settings(secret_key="x" * 32, auth_password="real-pw-123", api_token="").unsafe_settings() == []


def test_bad_number_names_the_variable(monkeypatch):
    monkeypatch.setenv("METRICS_PORT", "nine")
    with pytest.raises(ValueError, match="METRICS_PORT"):
        Settings()


def test_old_budget_default_secret_is_refused():
    s = Settings(secret_key="dev-secret-key-change-in-prod", auth_password="real-pw-123")
    assert s.unsafe_settings() == ["SECRET_KEY"]


def test_production_refuses_defaults():
    with pytest.raises(RuntimeError, match="SECRET_KEY, AUTH_PASSWORD"):
        KamiApp("demo", Settings(app_env="production", secret_key="short", auth_password="admin"))


def test_production_starts_with_real_secrets():
    s = Settings(app_env="production", secret_key="a-long-random-secret-value", auth_password="real-pw-123")
    app = KamiApp("demo", s).flask
    assert app.config["SESSION_COOKIE_NAME"] == "demo_session"
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_PATH"] == "/"
