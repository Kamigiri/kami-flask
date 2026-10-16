"""Serve the built Angular app: WhiteNoise for hashed assets, index.html for client routes.

Register it last: the catch-all route must not shadow the app's own routes, and /api/*
never falls through to index.html (unknown API paths answer a JSON 404).

Only a directory that contains ``index.html`` is served. An empty ``WEB_DIST`` would
otherwise become ``Path(".")`` and expose the working directory (source, .env, databases).
"""

from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, send_file
from whitenoise import WhiteNoise


def resolve_dist(dist: str | Path | None) -> Path | None:
    """The build directory to serve, or None when there is no usable build."""
    if dist is None or str(dist).strip() == "":
        return None
    path = Path(dist)
    return path if (path / "index.html").is_file() else None


def init(app: Flask, dist: str | Path | None) -> None:
    root = resolve_dist(dist)
    if root is not None:
        app.wsgi_app = WhiteNoise(app.wsgi_app, root=str(root))  # type: ignore[method-assign]

    @app.get("/", defaults={"path": ""}, endpoint="kami_spa")
    @app.get("/<path:path>", endpoint="kami_spa")
    def spa(path: str):
        if path == "api" or path.startswith("api/"):
            return jsonify({"error": "Not found"}), 404
        if root is not None:
            return send_file(root / "index.html")
        return jsonify({"error": "Frontend not built. Run `npm run build` in web/."}), 404
