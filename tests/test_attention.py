from datetime import UTC, datetime, timedelta, timezone

import pytest
from flask import Blueprint, jsonify

from kami_flask import KamiApp, Settings, attention
from kami_flask.testing import TEST_INTERNAL_TOKEN

URL = "/api/internal/attention"
AT = datetime(2026, 11, 27, 9, 30, tzinfo=UTC)
API_TOKEN = "demo-api-token-0123456789"


def item(item_id="msg:1", **overrides):
    values = {"id": item_id, "kind": "important-mail", "severity": "warn", "title": "Hello", "at": AT, "ackable": True}
    values.update(overrides)
    return attention.Item(**values)


def make_app(**overrides):
    settings = Settings(
        auth_username="tester",
        auth_password="test-password",
        secret_key="x" * 32,
        api_token=API_TOKEN,
        internal_token=TEST_INTERNAL_TOKEN,
    )
    for key, value in overrides.items():
        setattr(settings, key, value)
    kami = KamiApp("demo", settings)
    kami.serve_spa("/nonexistent-dist")
    return kami.flask


@pytest.fixture(autouse=True)
def clean_registry():
    attention.reset()
    yield
    attention.reset()


@pytest.fixture
def acked():
    """A provider with one ackable and one fixed item, plus an ack handler that records what it accepted."""
    seen = []
    attention.provider(lambda: [item(), item("review:month", ackable=False, severity="info", count=7)])

    @attention.ack_handler
    def ack(item_id):
        if item_id != "msg:1":
            return False
        seen.append(item_id)
        return True

    return seen


@pytest.fixture
def app(acked):
    return make_app()


def test_items_are_serialised(internal_client):
    resp = internal_client.get(URL)
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-store"
    body = resp.get_json()
    assert body["generatedAt"].endswith("Z")
    assert body["items"][0] == {
        "id": "msg:1",
        "kind": "important-mail",
        "severity": "warn",
        "title": "Hello",
        "detail": None,
        "link": None,
        "at": "2026-11-27T09:30:00Z",
        "ackable": True,
        "count": None,
    }
    assert body["items"][1]["count"] == 7


def test_token_missing_or_wrong(anon_client, app):
    assert anon_client.get(URL).status_code == 401
    assert anon_client.get(URL, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert anon_client.post(f"{URL}/msg:1/ack").status_code == 401


def test_session_api_token_and_gateway_do_not_open_it(client, app):
    assert client.get(URL).status_code == 401
    assert client.get(URL, headers={"Authorization": f"Bearer {API_TOKEN}"}).status_code == 401
    app.extensions["kami"].settings.gateway_secret = "g" * 32
    headers = {"Remote-User": "owner", "X-Kami-Gateway": "g" * 32}
    assert app.test_client().get(URL, headers=headers).status_code == 401


def test_internal_token_does_not_open_the_rest_of_the_api(internal_client):
    assert internal_client.get("/api/something").status_code == 401


def test_only_the_attention_subtree_is_exempt(anon_client):
    assert anon_client.get("/api/internal").status_code == 401
    assert anon_client.get("/api/internal/attention-more").status_code == 401


def test_ack(internal_client, acked):
    assert internal_client.post(f"{URL}/msg:1/ack").status_code == 204
    assert acked == ["msg:1"]


def test_ack_needs_no_xhr_header(app, acked):
    resp = app.test_client().post(f"{URL}/msg:1/ack", headers={"Authorization": f"Bearer {TEST_INTERNAL_TOKEN}"})
    assert resp.status_code == 204


def test_ack_unknown_id(internal_client, acked):
    assert internal_client.post(f"{URL}/msg:99/ack").status_code == 404
    assert acked == []


def test_ack_non_ackable_item(internal_client, acked):
    resp = internal_client.post(f"{URL}/review:month/ack")
    assert resp.status_code == 400
    assert resp.get_json()["error"]
    assert acked == []


def test_ack_without_handler():
    attention.provider(lambda: [item()])
    client = make_app().test_client()
    resp = client.post(f"{URL}/msg:1/ack", headers={"Authorization": f"Bearer {TEST_INTERNAL_TOKEN}"})
    assert resp.status_code == 404


def test_provider_raises(caplog):
    @attention.provider
    def broken():
        raise RuntimeError("db is gone")

    app = make_app()
    app.config["PROPAGATE_EXCEPTIONS"] = False
    resp = app.test_client().get(URL, headers={"Authorization": f"Bearer {TEST_INTERNAL_TOKEN}"})
    assert resp.status_code == 500
    assert resp.get_json() == {"error": "Internal server error"}


def test_no_provider_is_404():
    attention.reset()
    client = make_app().test_client()
    for headers in ({}, {"Authorization": f"Bearer {TEST_INTERNAL_TOKEN}"}):
        assert client.get(URL, headers=headers).status_code == 404
        assert client.post(f"{URL}/msg:1/ack", headers=headers).status_code == 404


def test_no_internal_token_is_404(acked):
    client = make_app(internal_token="").test_client()
    assert client.get(URL).status_code == 404
    assert client.get(URL, headers={"Authorization": "Bearer "}).status_code == 404
    assert client.post(f"{URL}/msg:1/ack").status_code == 404


def test_provider_declared_after_the_app_is_built():
    app = make_app()
    attention.provider(lambda: [])
    resp = app.test_client().get(URL, headers={"Authorization": f"Bearer {TEST_INTERNAL_TOKEN}"})
    assert resp.get_json()["items"] == []


def test_does_not_shadow_an_app_blueprint_on_api_internal():
    """An app may have its own /api/internal API with its own token (and exempt it from the login itself)."""
    attention.provider(lambda: [])
    own = Blueprint("own_internal", __name__, url_prefix="/api/internal")
    own.add_url_rule("/suggestions", "suggestions", lambda: jsonify([]))
    app = make_app()
    app.register_blueprint(own)
    client = app.test_client()
    # Still behind the kami login: attention exempts its own subtree only.
    assert client.get("/api/internal/suggestions").status_code == 401
    assert client.get("/api/internal/suggestions", headers={"Authorization": f"Bearer {API_TOKEN}"}).status_code == 200
    assert client.get(URL, headers={"Authorization": f"Bearer {TEST_INTERNAL_TOKEN}"}).status_code == 200


@pytest.mark.parametrize(("env", "token", "warned"), [("production", "", True), ("production", "t" * 32, False), ("development", "", False)])
def test_production_without_token_warns_but_starts(monkeypatch, env, token, warned):
    attention.provider(lambda: [])
    messages = []
    monkeypatch.setattr(attention.log, "warning", lambda msg, *args: messages.append(msg))
    make_app(app_env=env, internal_token=token, api_token="")
    assert bool(messages) is warned


def test_item_cuts_long_text():
    cut = item(title="a" * 200, detail="b" * 300)
    assert len(cut.title) == 120 and cut.title.endswith("…")
    assert len(cut.detail) == 200


def test_item_times_are_utc():
    berlin = timezone(timedelta(hours=1))
    assert item(at=datetime(2026, 11, 27, 10, 30, tzinfo=berlin)).to_dict()["at"] == "2026-11-27T09:30:00Z"
    assert item(at=datetime(2026, 11, 27, 9, 30)).to_dict()["at"] == "2026-11-27T09:30:00Z"


@pytest.mark.parametrize("bad", [{"id": ""}, {"id": "a/b"}, {"id": ".."}, {"severity": "high"}])
def test_item_rejects_bad_values(bad):
    values = {"id": "x", "kind": "k", "severity": "info", "title": "t", "at": AT}
    with pytest.raises(ValueError):
        attention.Item(**{**values, **bad})
