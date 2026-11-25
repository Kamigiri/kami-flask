"""The runner client against a local stub server, and the fake."""

import base64
import inspect
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from kami_flask.claude_runner import FakeRunner, RunnerClient, RunResult, image_block, text_block

TOKEN = "test-runner-token-0123456789"
LIMITS = {"five_hour": {"utilization": 0.2, "resetsAt": 1}, "seven_day": None, "at": 5}
ARGS = {"model": "claude-sonnet-5-5", "system": "s", "schema": {"type": "object"},
        "content": [text_block("hello")]}


class Stub(BaseHTTPRequestHandler):
    def _answer(self):
        server = self.server
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        server.seen.append((self.command, self.path, dict(self.headers), body))
        time.sleep(server.delay)
        self.send_response(server.status)
        if server.status == 302:
            self.send_header("Location", "/elsewhere")
        self.send_header("Content-Length", str(len(server.answer)))
        self.end_headers()
        self.wfile.write(server.answer)

    do_GET = do_POST = _answer

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    server.seen, server.status, server.delay = [], 200, 0
    server.answer = b"{}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def runner(server):
    return RunnerClient(f"http://127.0.0.1:{server.server_port}/", TOKEN, "demo")


def say(server, data, status=200):
    server.status, server.answer = status, json.dumps(data).encode()


def test_success(server, runner):
    say(server, {"ok": True, "output": {"a": 1}, "usage": {"input_tokens": 10, "output_tokens": 2},
                 "cost_usd": 0.01, "limits": LIMITS})
    result = runner.run(**ARGS, timeout_s=60)
    assert result == RunResult(True, output={"a": 1}, limits=LIMITS, usage={"input_tokens": 10, "output_tokens": 2},
                               cost_usd=0.01, status=200)
    command, path, headers, body = server.seen[0]
    assert (command, path) == ("POST", "/v1/run")
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {"client": "demo", **ARGS, "timeout_s": 60}


def test_a_failed_run(server, runner):
    say(server, {"ok": False, "error": "limit", "retryable": True, "limits": LIMITS})
    result = runner.run(**ARGS)
    assert (result.ok, result.error, result.retryable, result.output) == (False, "limit", True, None)
    assert result.limits == LIMITS


@pytest.mark.parametrize(("status", "error", "retryable"), [
    (401, "auth", True), (413, "runner", False), (422, "runner", False), (500, "runner", True),
    (503, "runner", True), (302, "runner", True),
])
def test_http_errors(server, runner, status, error, retryable):
    say(server, {"error": "x"}, status)
    result = runner.run(**ARGS)
    assert (result.ok, result.error, result.retryable, result.status) == (False, error, retryable, status)
    assert len(server.seen) == 1  # a redirect is not followed


def test_non_json_answer(server, runner):
    server.answer = b"<html>"
    assert runner.run(**ARGS).error == "runner"


def test_slow_runner_is_a_timeout(server):
    server.delay = 1
    runner = RunnerClient(f"http://127.0.0.1:{server.server_port}", TOKEN, "demo", queue_margin_s=-0.8)
    result = runner.run(**ARGS, timeout_s=1)
    assert (result.error, result.retryable) == ("timeout", True)


def test_unreachable(server):
    port = server.server_port
    server.shutdown()
    server.server_close()
    runner = RunnerClient(f"http://127.0.0.1:{port}", TOKEN, "demo", queue_margin_s=0)
    result = runner.run(**ARGS, timeout_s=10)
    assert (result.ok, result.error, result.retryable, result.status) == (False, "runner", True, None)
    assert runner.limits() is None


def test_not_configured():
    runner = RunnerClient("", "", "demo")
    assert not runner.configured
    assert runner.run(**ARGS) == RunResult(False, error="runner")
    assert runner.limits() is None


def test_limits(server, runner):
    say(server, LIMITS)
    assert runner.limits() == LIMITS
    assert server.seen[0][:2] == ("GET", "/v1/limits")
    say(server, {"error": "unauthorized"}, 401)
    assert runner.limits() is None


def test_token_is_not_in_repr_or_logs(server, runner, caplog):
    say(server, {"error": "x"}, 500)
    runner.run(**ARGS)
    assert TOKEN not in repr(runner) + caplog.text
    assert "hello" not in caplog.text


def test_blocks():
    assert text_block("x") == {"type": "text", "text": "x"}
    block = image_block(b"\xff\xd8\xff", "image/jpeg")
    assert block["media_type"] == "image/jpeg"
    assert base64.b64decode(block["data"]) == b"\xff\xd8\xff"


def test_fake_runner():
    refused, good = RunResult(False, error="refused"), RunResult(True, output={"a": 1})
    fake = FakeRunner(refused, good)
    assert [fake.run(**ARGS) for _ in range(3)] == [refused, good, good]
    assert fake.calls[0] == {**ARGS, "timeout_s": 180}
    assert FakeRunner().run(**ARGS) == RunResult(True, output={})
    assert fake.limits() == {"five_hour": None, "seven_day": None, "at": None}


def test_fake_matches_the_client():
    for name in ("run", "limits"):
        assert inspect.signature(getattr(FakeRunner, name)) == inspect.signature(getattr(RunnerClient, name))
    assert FakeRunner().configured
