"""Prometheus metrics, served on a separate port (``METRICS_PORT``).

The port is never proxied by nginx, so metrics stay inside the Docker network. Counters
live in this process, which is why the apps run one gunicorn worker (with threads).

Apps add their own metrics on :data:`REGISTRY`::

    from prometheus_client import Counter
    from kami_flask.metrics import REGISTRY
    SYNCED = Counter("transactions_synced_total", "...", ["bank"], registry=REGISTRY)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from flask import Flask, Response, g, request
from prometheus_client import (
    GC_COLLECTOR,
    PLATFORM_COLLECTOR,
    PROCESS_COLLECTOR,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    Info,
    generate_latest,
    start_http_server,
)

from .logs import route_of

if TYPE_CHECKING:
    from .app import KamiApp

log = logging.getLogger("kami.metrics")

REGISTRY = CollectorRegistry(auto_describe=True)
for _collector in (PROCESS_COLLECTOR, PLATFORM_COLLECTOR, GC_COLLECTOR):
    REGISTRY.register(_collector)

APP_INFO = Info("app", "App name, version and git revision", registry=REGISTRY)
HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests by route and status", ["method", "route", "status"], registry=REGISTRY
)
HTTP_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 120),
    registry=REGISTRY,
)
HTTP_IN_FLIGHT = Gauge("http_requests_in_flight", "Requests being handled", registry=REGISTRY)
APP_ERRORS = Counter("app_errors_total", "Unhandled exceptions", registry=REGISTRY)
JOB_RUNS = Counter("job_runs_total", "Background job runs", ["job", "result"], registry=REGISTRY)
JOB_DURATION = Histogram(
    "job_duration_seconds",
    "Background job duration",
    ["job"],
    buckets=(0.1, 0.5, 1, 5, 15, 30, 60, 120, 300, 600, 1800),
    registry=REGISTRY,
)
JOB_LAST_SUCCESS = Gauge(
    "job_last_success_timestamp", "Unix time of the last successful run", ["job"], registry=REGISTRY
)

_server_lock = threading.Lock()
_server_port: int | None = None


def start_server(port: int) -> bool:
    """Start the metrics HTTP server once per process.

    A second process in the same container (a CLI script calling create_app while gunicorn
    holds the port) gets a warning instead of a crash. Returns whether this process serves.
    """
    global _server_port
    with _server_lock:
        if _server_port is not None:
            return True
        try:
            start_http_server(port, registry=REGISTRY)
        except OSError as exc:
            log.warning("metrics port %s unavailable (%s); not serving metrics from this process", port, exc)
            return False
        _server_port = port
        return True


def render() -> bytes:
    return generate_latest(REGISTRY)


def init(app: Flask, kami: KamiApp) -> None:
    APP_INFO.info({"app": kami.name, "version": kami.version, "revision": kami.settings.revision})

    @app.before_request
    def _in_flight():
        g.kami_in_flight = True
        HTTP_IN_FLIGHT.inc()

    @app.after_request
    def _observe(response: Response) -> Response:
        route = route_of()
        HTTP_REQUESTS.labels(request.method, route, str(response.status_code)).inc()
        start = g.get("kami_start")
        if start is not None:
            HTTP_DURATION.labels(request.method, route).observe(time.perf_counter() - start)
        return response

    @app.teardown_request
    def _done(_exc):
        if g.pop("kami_in_flight", False):
            HTTP_IN_FLIGHT.dec()

    if kami.settings.metrics_port:
        start_server(kami.settings.metrics_port)
