import pytest
from flask import Blueprint, abort, jsonify

from kami_flask import KamiApp, Settings

demo = Blueprint("demo", __name__, url_prefix="/api")


@demo.get("/items/<int:item_id>")
def get_item(item_id: int):
    return jsonify({"id": item_id})


@demo.post("/items")
def create_item():
    return jsonify({"created": True}), 201


@demo.get("/custom")
def custom():
    abort(400, "name is required")


@demo.get("/plain")
def plain():
    abort(404)


@demo.get("/boom")
def boom():
    raise ValueError("kaboom")


def make_app(tmp_path=None, **overrides):
    settings = Settings(auth_username="tester", auth_password="test-password", secret_key="x" * 32)
    for key, value in overrides.items():
        setattr(settings, key, value)
    kami = KamiApp("demo", settings, version="1.2.3")
    kami.flask.register_blueprint(demo)
    if tmp_path is None:
        kami.serve_spa("/nonexistent-dist")
    else:
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<html>spa</html>")
        (dist / "main-ABC.js").write_text("console.log(1)")
        kami.serve_spa(dist)
    return kami


@pytest.fixture
def kami(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def app(kami):
    kami.flask.config["TESTING"] = True
    return kami.flask
