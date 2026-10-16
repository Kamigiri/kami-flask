from kami_flask.testing import TEST_GATEWAY_SECRET, TEST_GATEWAY_USER, make_client


def test_gateway_user_with_secret(gateway_client):
    assert gateway_client.get("/api/items/1").status_code == 200
    me = gateway_client.get("/api/auth/me").get_json()
    assert me == {"username": TEST_GATEWAY_USER, "auth": "gateway", "logout_url": ""}


def test_logout_url_reported(app, gateway_client):
    app.extensions["kami"].settings.gateway_logout_url = "https://example.test/logout"
    assert gateway_client.get("/api/auth/me").get_json()["logout_url"] == "https://example.test/logout"


def test_remote_user_without_secret_is_ignored(app, gateway_client):
    raw = app.test_client()
    assert raw.get("/api/items/1", headers={"Remote-User": "owner"}).status_code == 401
    assert raw.get("/api/auth/me", headers={"Remote-User": "owner"}).status_code == 401


def test_wrong_secret_is_rejected(app, gateway_client):
    raw = app.test_client()
    headers = {"Remote-User": "owner", "X-Kami-Gateway": "wrong-" + TEST_GATEWAY_SECRET}
    assert raw.get("/api/items/1", headers=headers).status_code == 401


def test_gateway_off_when_no_secret_configured(app):
    raw = app.test_client()
    assert raw.get("/api/items/1", headers={"Remote-User": "owner", "X-Kami-Gateway": ""}).status_code == 401


def test_empty_remote_user_falls_through(app, gateway_client):
    raw = app.test_client()
    headers = {"Remote-User": "", "X-Kami-Gateway": TEST_GATEWAY_SECRET}
    assert raw.get("/api/items/1", headers=headers).status_code == 401
    # ...to the session, when there is one
    raw.post("/api/auth/login", json={"username": "tester", "password": "test-password"})
    assert raw.get("/api/items/1", headers=headers).status_code == 200
    assert raw.get("/api/auth/me", headers=headers).get_json()["auth"] == "session"


def test_gateway_mutation_needs_xhr_header(app, gateway_client):
    raw = app.test_client()
    headers = {"Remote-User": "owner", "X-Kami-Gateway": TEST_GATEWAY_SECRET}
    assert raw.post("/api/items", headers=headers).status_code == 403
    assert raw.post("/api/items", headers={**headers, "X-Requested-With": "XMLHttpRequest"}).status_code == 201


def test_bearer_and_session_still_work_next_to_gateway(app, gateway_client):
    app.extensions["kami"].settings.api_token = "t" * 20
    assert make_client(app, bearer="t" * 20).get("/api/auth/me").get_json()["auth"] == "token"
    assert make_client(app, login=True).get("/api/auth/me").get_json()["auth"] == "session"


def test_short_gateway_secret_is_unsafe(kami):
    kami.settings.gateway_secret = "short"
    assert "GATEWAY_SECRET" in kami.settings.unsafe_settings()
    kami.settings.gateway_secret = ""
    assert "GATEWAY_SECRET" not in kami.settings.unsafe_settings()
    kami.settings.gateway_secret = "x" * 32
    assert "GATEWAY_SECRET" not in kami.settings.unsafe_settings()
