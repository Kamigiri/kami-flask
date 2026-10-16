"""The platform contract read by compose healthchecks, Prometheus and Pulse.

- ``/api/health``: liveness, always 200 while the process serves requests.
- ``/api/ready``: runs the app's ``ready_check`` (e.g. a DB query); 503 when it fails.
- ``/api/version``: app, version, git revision and build time (set by CI in the image).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from flask import Flask, jsonify

if TYPE_CHECKING:
    from .app import KamiApp

log = logging.getLogger("kami.health")


def init(app: Flask, kami: KamiApp) -> None:
    def health():
        return jsonify({"status": "ok", "app": kami.name, "version": kami.version})

    def ready():
        if kami.ready_check is not None:
            try:
                kami.ready_check()
            except Exception:
                # /api/ready is public: details (SQL, paths) go to the log, not the response.
                log.warning("ready check failed", exc_info=True)
                return jsonify({"status": "unavailable", "error": "dependency unavailable"}), 503
        return jsonify({"status": "ready"})

    def version():
        return jsonify(
            {
                "app": kami.name,
                "version": kami.version,
                "revision": kami.settings.revision,
                "built_at": kami.settings.built_at,
            }
        )

    app.add_url_rule("/api/health", "kami_health", health, methods=["GET"])
    app.add_url_rule("/api/ready", "kami_ready", ready, methods=["GET"])
    app.add_url_rule("/api/version", "kami_version", version, methods=["GET"])
