import json
import logging

import pytest

from kami_flask import job, metrics
from kami_flask.logs import JsonFormatter

ITEM_ROUTE = "/api/items/<int:item_id>"


def _value(name, **labels):
    return metrics.REGISTRY.get_sample_value(name, labels) or 0


def test_request_metrics_use_the_route_rule(client):
    before = _value("http_requests_total", method="GET", route=ITEM_ROUTE, status="200")
    client.get("/api/items/1")
    client.get("/api/items/2")
    assert _value("http_requests_total", method="GET", route=ITEM_ROUTE, status="200") - before == 2
    assert _value("http_requests_in_flight") == 0
    assert b"app_info" in metrics.render()


def test_errors_are_counted(client):
    before = _value("app_errors_total")
    client.get("/api/boom")
    assert _value("app_errors_total") - before == 1


def test_request_log_line_and_request_id(client, caplog):
    caplog.set_level(logging.INFO, logger="kami.request")
    resp = client.get("/api/items/3", headers={"X-Request-ID": "req-123"})
    assert resp.headers["X-Request-ID"] == "req-123"
    record = next(r for r in caplog.records if r.name == "kami.request")
    assert (record.route, record.status, record.request_id, record.user) == (ITEM_ROUTE, 200, "req-123", "tester")
    line = json.loads(JsonFormatter("demo").format(record))
    assert line["app"] == "demo"
    assert line["route"] == ITEM_ROUTE
    assert line["duration_ms"] >= 0


def test_probes_log_at_debug(anon_client, caplog):
    caplog.set_level(logging.INFO, logger="kami.request")
    anon_client.get("/api/health")
    assert not [r for r in caplog.records if r.name == "kami.request"]


def test_job_decorator_and_context_manager():
    @job("demo_ok")
    def ok():
        return 42

    before = _value("job_runs_total", job="demo_ok", result="ok")
    assert ok() == 42
    assert _value("job_runs_total", job="demo_ok", result="ok") - before == 1
    assert _value("job_last_success_timestamp", job="demo_ok") > 0

    with pytest.raises(ValueError):
        with job("demo_fail"):
            raise ValueError("nope")
    assert _value("job_runs_total", job="demo_fail", result="error") >= 1


def test_metrics_port_in_use_warns_instead_of_crashing(monkeypatch, caplog):
    import socket

    monkeypatch.setattr(metrics, "_server_port", None)
    with socket.socket() as busy:
        busy.bind(("0.0.0.0", 0))
        busy.listen()
        port = busy.getsockname()[1]
        caplog.set_level(logging.WARNING, logger="kami.metrics")
        assert metrics.start_server(port) is False
    assert "unavailable" in caplog.text


def test_long_request_id_is_capped(client):
    resp = client.get("/api/items/1", headers={"X-Request-ID": "x" * 500})
    assert len(resp.headers["X-Request-ID"]) == 64


def test_reused_job_instance_starts_clean():
    run = job("demo_reused")
    with run:
        run.mark("error")
    with run:
        pass
    assert _value("job_runs_total", job="demo_reused", result="ok") == 1


def test_job_marked_result_is_not_a_success():
    with job("demo_marked") as run:
        run.mark("auth_required")
    assert _value("job_runs_total", job="demo_marked", result="auth_required") == 1
    assert _value("job_last_success_timestamp", job="demo_marked") == 0
