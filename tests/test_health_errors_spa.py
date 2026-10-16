from conftest import make_app


def test_health_and_version(anon_client):
    assert anon_client.get("/api/health").get_json() == {"status": "ok", "app": "demo", "version": "1.2.3"}
    assert anon_client.get("/api/version").get_json() == {
        "app": "demo",
        "version": "1.2.3",
        "revision": "dev",
        "built_at": "dev",
    }


def test_ready_reports_failures(anon_client, kami):
    def broken():
        raise RuntimeError("db down")

    kami.ready_check = broken
    resp = anon_client.get("/api/ready")
    assert resp.status_code == 503
    assert resp.get_json() == {"status": "unavailable", "error": "dependency unavailable"}  # no detail leak


def test_unhandled_error_is_json_and_runs_hooks(client, kami):
    seen = []
    kami.on_error(seen.append)
    resp = client.get("/api/boom")
    assert resp.status_code == 500
    assert resp.get_json() == {"error": "Internal server error"}
    assert isinstance(seen[0], ValueError)


def test_http_errors_are_json(client):
    resp = client.get("/api/nothing-here")
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Not found"}
    resp = client.delete("/api/items/1")
    assert resp.status_code == 405
    assert "error" in resp.get_json()
    assert "GET" in resp.headers["Allow"]


def test_spa_fallback_and_assets(anon_client):
    assert anon_client.get("/today").data == b"<html>spa</html>"
    assert anon_client.get("/").data == b"<html>spa</html>"
    assert b"console.log" in anon_client.get("/main-ABC.js").data


def test_abort_messages(client):
    assert client.get("/api/custom").get_json() == {"error": "name is required"}
    assert client.get("/api/plain").get_json() == {"error": "Not Found"}  # not werkzeug's long sentence


def test_empty_web_dist_never_serves_the_working_directory(tmp_path, monkeypatch):
    (tmp_path / "secret.env").write_text("TOKEN=x")
    (tmp_path / "app.db").write_text("data")
    monkeypatch.chdir(tmp_path)
    for dist in ("", "   ", None):
        app = make_app(None, web_dist="").flask if dist is None else _app_with_dist(dist)
        client = app.test_client()
        assert client.get("/secret.env").status_code == 404
        assert client.get("/app.db").status_code == 404


def test_dist_without_index_is_not_served(tmp_path, monkeypatch):
    (tmp_path / "config.py").write_text("SECRET = 1")
    app = _app_with_dist(str(tmp_path))
    assert app.test_client().get("/config.py").status_code == 404


def _app_with_dist(dist):
    from kami_flask import KamiApp, Settings

    kami = KamiApp("demo", Settings(web_dist=dist))
    kami.serve_spa()
    return kami.flask


def test_spa_missing_build():
    app = make_app(None).flask
    resp = app.test_client().get("/today")
    assert resp.status_code == 404
    assert "not built" in resp.get_json()["error"]
