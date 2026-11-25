"""Client for the platform's internal Claude runner service (headless Claude Code, no tools), plus a fake for tests.

Standard library only. ``run()`` never raises for an expected failure: it returns a :class:`RunResult` whose ``error``
is one of ``refused | invalid_output | limit | auth | timeout | cli`` (from the runner) or ``runner`` (the runner is
unreachable, busy, or refused the request). Nothing here logs prompts, images or output.

Usage::

    from kami_flask.claude_runner import RunnerClient, image_block, text_block

    runner = RunnerClient(settings.claude_runner_url, settings.claude_runner_token, client="tasks")
    result = runner.run(model="claude-sonnet-5-5", system=PROMPT, schema=SCHEMA,
                        content=[image_block(jpeg_bytes, "image/jpeg"), text_block("Today is ...")], timeout_s=180)
    if result.ok:
        use(result.output)          # validate it again: it is model output
    elif result.retryable:
        retry_later(result.error, result.limits)

App tests pass a :class:`FakeRunner` instead of the client.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

__all__ = ["FakeRunner", "RunResult", "RunnerClient", "image_block", "text_block"]

log = logging.getLogger("kami.claude_runner")

# How long one request may wait in the runner's queue (300 s there) before its own run starts, plus some slack.
QUEUE_MARGIN_S = 330.0


@dataclass(frozen=True)
class RunResult:
    ok: bool
    output: Any = None
    error: str | None = None
    retryable: bool = False
    # {"five_hour": {"utilization", "resetsAt"} | None, "seven_day": ... | None, "at": int | None}; {} without an answer
    limits: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)  # {"input_tokens", "output_tokens"} of a successful run
    cost_usd: float | None = None
    status: int | None = None  # HTTP status, None when the runner did not answer


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def image_block(data: bytes, media_type: str) -> dict:
    """``image/jpeg``, ``image/png`` or ``image/webp``, at most 5 MB (4 per run): downscale before calling."""
    return {"type": "image", "media_type": media_type, "data": base64.b64encode(data).decode("ascii")}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # the runner never redirects; do not carry the token anywhere else


# No proxies: the runner is a container on the same Docker network.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


class RunnerClient:
    def __init__(self, url: str, token: str, client: str, *, queue_margin_s: float = QUEUE_MARGIN_S) -> None:
        """``client``: the name the token was issued for. An empty ``url`` or ``token`` means "not configured"."""
        self.url = (url or "").rstrip("/")
        self.token = token or ""
        self.client = client
        self.queue_margin_s = queue_margin_s

    def __repr__(self) -> str:  # never show the token
        return f"RunnerClient({self.url!r}, client={self.client!r})"

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    def _call(self, path: str, timeout: float, payload: bytes | None = None) -> tuple[int, object]:
        request = urllib.request.Request(self.url + path, data=payload, method="POST" if payload else "GET")
        request.add_header("Authorization", f"Bearer {self.token}")
        if payload:
            request.add_header("Content-Type", "application/json")
        try:
            with _OPENER.open(request, timeout=timeout) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as err:
            err.close()
            return err.code, None

    def run(self, *, model: str, system: str, schema: dict, content: list[dict], timeout_s: int = 180) -> RunResult:
        """One ``POST /v1/run``. Blocks for up to ``timeout_s`` plus the time in the runner's queue."""
        if not self.configured:
            return RunResult(False, error="runner")  # retrying cannot help
        payload = json.dumps({"client": self.client, "model": model, "system": system, "schema": schema,
                              "content": content, "timeout_s": timeout_s}).encode("utf-8")
        try:
            status, data = self._call("/v1/run", timeout_s + self.queue_margin_s, payload)
        except TimeoutError:
            return RunResult(False, error="timeout", retryable=True)
        except urllib.error.URLError as err:
            if isinstance(err.reason, TimeoutError):
                return RunResult(False, error="timeout", retryable=True)
            return self._unreachable(err)
        except (OSError, ValueError) as err:  # reset connection, non-JSON answer, broken URL
            return self._unreachable(err)
        if status == 401:  # the owner has to fix the token; retrying later is right once they did
            return RunResult(False, error="auth", retryable=True, status=status)
        if status != 200 or not isinstance(data, dict):
            log.warning("claude runner answered HTTP %s", status)
            # 413 / 422 would fail the same way again; 503 (busy) and 5xx may not.
            return RunResult(False, error="runner", retryable=status not in (413, 422), status=status)
        return _result(data, status)

    def limits(self) -> dict | None:
        """``GET /v1/limits``; None when the runner cannot be asked."""
        if not self.configured:
            return None
        try:
            status, data = self._call("/v1/limits", 10)
        except (OSError, ValueError):
            return None
        return data if status == 200 and isinstance(data, dict) else None

    @staticmethod
    def _unreachable(err: Exception) -> RunResult:
        log.warning("claude runner unreachable (%s)", type(err).__name__)
        return RunResult(False, error="runner", retryable=True)


def _result(data: dict, status: int) -> RunResult:
    ok = data.get("ok") is True
    return RunResult(
        ok=ok,
        output=data.get("output") if ok else None,
        error=None if ok else str(data.get("error") or "runner"),
        retryable=False if ok else bool(data.get("retryable")),
        limits=data.get("limits") or {},
        usage=data.get("usage") or {},
        cost_usd=data.get("cost_usd"),
        status=status,
    )


class FakeRunner:
    """Test double for :class:`RunnerClient`: answers with the given results in order (the last one repeats) and records
    each call's arguments in ``calls``. Without results every run succeeds with an empty object.

    ::

        FakeRunner(RunResult(False, error="limit", retryable=True), RunResult(True, output={"titles": []}))
    """

    configured = True

    def __init__(self, *results: RunResult) -> None:
        self.results = list(results) or [RunResult(True, output={})]
        self.calls: list[dict[str, Any]] = []

    def run(self, *, model: str, system: str, schema: dict, content: list[dict], timeout_s: int = 180) -> RunResult:
        self.calls.append({"model": model, "system": system, "schema": schema, "content": content,
                           "timeout_s": timeout_s})
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]

    def limits(self) -> dict | None:
        return {"five_hour": None, "seven_day": None, "at": None}
