from kami_flask import auth


def test_api_needs_login(anon_client):
    resp = anon_client.get("/api/items/1")
    assert resp.status_code == 401
    assert resp.get_json() == {"error": "Not authenticated"}


def test_open_paths(anon_client):
    for path in ("/api/health", "/api/ready", "/api/version"):
        assert anon_client.get(path).status_code == 200, path


def test_login_me_logout(anon_client):
    bad = anon_client.post("/api/auth/login", json={"username": "tester", "password": "nope"})
    assert bad.status_code == 401
    good = anon_client.post("/api/auth/login", json={"username": "tester", "password": "test-password"})
    assert good.status_code == 200
    assert anon_client.get("/api/auth/me").get_json() == {"username": "tester", "auth": "session"}
    assert anon_client.get("/api/items/1").status_code == 200
    anon_client.post("/api/auth/logout")
    assert anon_client.get("/api/items/1").status_code == 401


def test_client_fixture_is_logged_in(client):
    assert client.get("/api/items/7").get_json() == {"id": 7}
    assert client.post("/api/items").status_code == 201


def test_cookie_mutation_needs_xhr_header(app):
    raw = app.test_client()  # plain FlaskClient, no automatic header
    raw.post("/api/auth/login", json={"username": "tester", "password": "test-password"})
    assert raw.post("/api/items").status_code == 403
    assert raw.post("/api/items", headers={"X-Requested-With": "XMLHttpRequest"}).status_code == 201


def test_bearer_token(bearer_client, app):
    assert bearer_client.get("/api/items/1").status_code == 200
    assert bearer_client.get("/api/auth/me").get_json()["auth"] == "token"
    assert bearer_client.post("/api/items").status_code == 201
    raw = app.test_client()
    assert raw.get("/api/items/1", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_empty_bearer_without_token_configured(anon_client):
    assert anon_client.get("/api/items/1", headers={"Authorization": "Bearer "}).status_code == 401


def test_anonymous_api_paths_never_reach_the_spa(anon_client):
    for path in ("/api/anything", "/api/items/1", "/api"):
        resp = anon_client.get(path)
        assert resp.status_code == 401, path
        assert b"spa" not in resp.data


def test_logout_needs_xhr_header_when_cookie_authed(app):
    raw = app.test_client()
    raw.post("/api/auth/login", json={"username": "tester", "password": "test-password"})
    assert raw.post("/api/auth/logout").status_code == 403
    assert raw.post("/api/auth/logout", headers={"X-Requested-With": "XMLHttpRequest"}).status_code == 200


def test_login_clears_the_old_session(app):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["planted"] = "attacker"
    client.post("/api/auth/login", json={"username": "tester", "password": "test-password"})
    with client.session_transaction() as sess:
        assert "planted" not in sess
        assert sess["auth_user"] == "tester"


def test_throttle_uses_the_forwarded_client_behind_a_trusted_proxy():
    from conftest import make_app

    app = make_app(None, trusted_proxies=1).flask
    client = app.test_client()

    def login(password, ip):
        return client.post(
            "/api/auth/login",
            json={"username": "tester", "password": password},
            headers={"X-Forwarded-For": ip},
        )

    for _ in range(auth.THROTTLE_MAX_FAILURES):
        login("nope", "203.0.113.7")
    assert login("nope", "203.0.113.7").status_code == 429
    assert login("test-password", "198.51.100.2").status_code == 200  # another client is fine


def test_forwarded_header_ignored_without_trusted_proxy():
    from conftest import make_app

    client = make_app(None).flask.test_client()
    for n in range(auth.THROTTLE_MAX_FAILURES):
        client.post("/api/auth/login", json={"username": "tester", "password": "nope"}, headers={"X-Forwarded-For": f"203.0.113.{n}"})
    resp = client.post("/api/auth/login", json={"username": "tester", "password": "nope"}, headers={"X-Forwarded-For": "1.2.3.4"})
    assert resp.status_code == 429


def test_login_throttle(anon_client):
    for _ in range(auth.THROTTLE_MAX_FAILURES):
        anon_client.post("/api/auth/login", json={"username": "tester", "password": "nope"})
    resp = anon_client.post("/api/auth/login", json={"username": "tester", "password": "test-password"})
    assert resp.status_code == 429
