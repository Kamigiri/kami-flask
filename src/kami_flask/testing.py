"""pytest plugin (auto-loaded via the ``pytest11`` entry point).

The app's own ``conftest.py`` defines an ``app`` fixture (DB setup differs per app); the
fixtures here build on it:

- ``client``: logged in with the app's credentials, sends ``X-Requested-With`` like the SPA.
- ``anon_client``: same client class, not logged in.
- ``bearer_client``: no session, ``Authorization: Bearer <api token>`` on every request.

``app.test_client()`` stays a plain FlaskClient, for tests that need the raw behaviour
(e.g. checking the CSRF header requirement).
"""

from __future__ import annotations

import pytest
from flask import Flask
from flask.testing import FlaskClient
from werkzeug.datastructures import Headers

TEST_API_TOKEN = "kami-test-api-token-0123456789"


class KamiTestClient(FlaskClient):
    """Sends the XHR header (and optional extra headers) on every request, like the SPA."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.extra_headers: dict[str, str] = {}

    def open(self, *args, **kwargs):
        headers = Headers(kwargs.pop("headers", None) or {})
        headers.setdefault("X-Requested-With", "XMLHttpRequest")
        for key, value in self.extra_headers.items():
            headers.setdefault(key, value)
        kwargs["headers"] = headers
        return super().open(*args, **kwargs)


def make_client(app: Flask, *, login: bool = False, bearer: str | None = None) -> KamiTestClient:
    client = KamiTestClient(app, app.response_class, use_cookies=True)
    if bearer:
        client.extra_headers = {"Authorization": f"Bearer {bearer}"}
    if login:
        settings = app.extensions["kami"].settings
        resp = client.post(
            "/api/auth/login", json={"username": settings.auth_username, "password": settings.auth_password}
        )
        assert resp.status_code == 200, resp.get_json()
    return client


@pytest.fixture
def client(app: Flask) -> KamiTestClient:
    return make_client(app, login=True)


@pytest.fixture
def anon_client(app: Flask) -> KamiTestClient:
    return make_client(app)


@pytest.fixture
def bearer_client(app: Flask) -> KamiTestClient:
    settings = app.extensions["kami"].settings
    if not settings.api_token:
        settings.api_token = TEST_API_TOKEN
    return make_client(app, bearer=settings.api_token)
