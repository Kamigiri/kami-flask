"""Track background and scheduled work: metrics + a start/end log line per run.

Use as a decorator or a context manager::

    @job("google_sync")
    def run_sync(): ...

    with job("csv_report"):
        ...

Exceptions are recorded (``result="error"``) and re-raised, so callers keep their own
error handling. Work that reports failure without raising sets the result itself::

    with job("sync_all") as run:
        result = sync()
        if result["status"] != "completed":
            run.mark(result["status"])   # e.g. "auth_required"; only "ok" counts as success
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable
from typing import Any

from .metrics import JOB_DURATION, JOB_LAST_SUCCESS, JOB_RUNS

log = logging.getLogger("kami.job")


class job:  # noqa: N801 (reads like a function at the call site)
    """One run per ``with`` block; as a decorator every call gets a fresh run."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._start = 0.0
        self.result: str | None = None

    def __call__(self, func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with job(self.name):  # a fresh run per call (thread-safe)
                return func(*args, **kwargs)

        return wrapper

    def mark(self, result: str) -> None:
        """Record a result other than "ok" for a run that did not raise."""
        self.result = result

    def __enter__(self) -> job:
        self._start = time.perf_counter()
        self.result = None  # a reused instance starts clean
        log.info("job %s started", self.name, extra={"job": self.name})
        return self

    def __exit__(self, exc_type, exc, _tb) -> bool:
        duration = time.perf_counter() - self._start
        result = "error" if exc_type else (self.result or "ok")
        JOB_RUNS.labels(self.name, result).inc()
        JOB_DURATION.labels(self.name).observe(duration)
        extra = {"job": self.name, "result": result, "duration_ms": round(duration * 1000, 1)}
        if exc_type:
            # No traceback here: the exception is re-raised and the caller logs it.
            log.error("job %s failed: %s: %s", self.name, exc_type.__name__, exc, extra=extra)
        elif result != "ok":
            log.warning("job %s ended with %s", self.name, result, extra=extra)
        else:
            JOB_LAST_SUCCESS.labels(self.name).set_to_current_time()
            log.info("job %s finished in %.1fs", self.name, duration, extra=extra)
        return False
