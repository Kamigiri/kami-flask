"""Logging: JSON lines on stdout in production (Loki-friendly), readable text in dev.

Every request produces one log line with method, route, status, duration and the request
id (taken from nginx's ``X-Request-ID`` or generated, and echoed in the response).
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from flask import Flask, Response, g, request

if TYPE_CHECKING:
    from .app import KamiApp

REQUEST_FIELDS = ("request_id", "method", "route", "path", "status", "duration_ms", "user", "job", "result")
QUIET_PATHS = {"/api/health", "/api/ready", "/api/version"}  # probes: DEBUG, not INFO
NOISY_LOGGERS = ("werkzeug", "urllib3", "googleapiclient", "apscheduler")  # WARNING and up
MAX_REQUEST_ID = 64

request_log = logging.getLogger("kami.request")


class JsonFormatter(logging.Formatter):
    def __init__(self, app_name: str) -> None:
        super().__init__()
        self.app_name = app_name

    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "app": self.app_name,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in REQUEST_FIELDS:
            value = getattr(record, key, None)
            if value is not None:
                data[key] = value
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)


def setup_logging(app_name: str, fmt: str, level: str) -> None:
    """Route every logger (gunicorn, werkzeug, the app's) through one stdout handler."""
    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter(app_name))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level.upper())
    for name in ("gunicorn.error", "werkzeug"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    for name in NOISY_LOGGERS:  # the dev server's own access line duplicates kami.request
        logging.getLogger(name).setLevel(logging.WARNING)
    # gunicorn's own access log would duplicate the request line below
    logging.getLogger("gunicorn.access").disabled = True


def request_context() -> dict:
    """Fields that tie a log line to its request (for error logs outside the request line)."""
    return {
        "request_id": g.get("request_id"),
        "method": request.method,
        "route": route_of(),
        "path": request.path,
        "user": g.get("kami_user"),
    }


def route_of() -> str:
    """The URL rule (bounded cardinality for logs and metrics), not the raw path."""
    return request.url_rule.rule if request.url_rule else "<unmatched>"


def init(app: Flask, kami: KamiApp) -> None:
    @app.before_request
    def _start():
        g.kami_start = time.perf_counter()
        # nginx sets it; direct clients could send anything, so cap the length
        g.request_id = (request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16])[:MAX_REQUEST_ID]

    @app.after_request
    def _log(response: Response) -> Response:
        start = g.get("kami_start")
        duration_ms = round((time.perf_counter() - start) * 1000, 1) if start else None
        response.headers.setdefault("X-Request-ID", g.get("request_id", ""))
        level = logging.DEBUG if request.path in QUIET_PATHS else logging.INFO
        request_log.log(
            level,
            "%s %s %s %sms",
            request.method,
            request.path,
            response.status_code,
            duration_ms,
            extra={
                "request_id": g.get("request_id"),
                "method": request.method,
                "route": route_of(),
                "path": request.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
                "user": g.get("kami_user"),
            },
        )
        return response
