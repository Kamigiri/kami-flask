"""The "needs attention" contract: an app says what its owner has to look at right now.

An app declares one provider (and optionally one ack handler)::

    from kami_flask import attention

    @attention.provider
    def items() -> list[attention.Item]: ...

    @attention.ack_handler          # optional; True = acked, False = unknown id
    def ack(item_id: str) -> bool: ...

and gets ``GET /api/internal/attention`` and ``POST /api/internal/attention/<id>/ack``. They are meant for the
platform's front door on the internal network: the proxy must not expose them. Auth is Bearer ``INTERNAL_TOKEN``
only; a session, ``API_TOKEN`` or the gateway do not open them. Without a provider or without a token they answer 404.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from flask import Blueprint, Flask, abort, current_app, jsonify, request

from .auth import same

PREFIX = "/api/internal/attention"
SEVERITIES = ("info", "warn", "urgent")
TITLE_MAX = 120
DETAIL_MAX = 200

log = logging.getLogger("kami.attention")

bp = Blueprint("kami_attention", __name__, url_prefix=PREFIX)

_provider: Callable[[], list[Item]] | None = None
_ack_handler: Callable[[str], bool] | None = None


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _iso(moment: datetime) -> str:
    # Naive values are taken as UTC (SQLite returns them that way).
    moment = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Item:
    """One thing that needs the owner. ``title`` and ``detail`` are plain text, never a secret or a URL."""

    id: str  # stable and app-scoped, e.g. "msg:123"; no "/"
    kind: str  # e.g. "review", "budget"
    severity: str  # "info" | "warn" | "urgent"
    title: str  # cut to 120 characters
    at: datetime  # UTC
    detail: str | None = None  # cut to 200 characters
    link: str | None = None  # path inside the app, relative to its root, e.g. "review"
    ackable: bool = False
    count: int | None = None

    def __post_init__(self) -> None:
        if not self.id or "/" in self.id or self.id in {".", ".."}:
            raise ValueError(f"attention item id must be non-empty and without '/', got {self.id!r}")
        if self.severity not in SEVERITIES:
            raise ValueError(f"attention severity must be one of {', '.join(SEVERITIES)}, got {self.severity!r}")
        # Cut instead of failing: one long mail subject must not take the whole feed down.
        object.__setattr__(self, "title", _clip(self.title, TITLE_MAX))
        if self.detail is not None:
            object.__setattr__(self, "detail", _clip(self.detail, DETAIL_MAX))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "link": self.link,
            "at": _iso(self.at),
            "ackable": self.ackable,
            "count": self.count,
        }


def provider(fn: Callable[[], list[Item]]) -> Callable[[], list[Item]]:
    """Declare the app's provider (one per process). It runs in a request, so it may use the app context."""
    global _provider
    _provider = fn
    return fn


def ack_handler(fn: Callable[[str], bool]) -> Callable[[str], bool]:
    """Declare how "Got it" is stored. Return False for an unknown id."""
    global _ack_handler
    _ack_handler = fn
    return fn


def reset() -> None:
    """Forget the provider and the ack handler (for tests: the registration is per process)."""
    global _provider, _ack_handler
    _provider = _ack_handler = None


def warn_if_off(app: Flask) -> None:
    """In production a provider without a token is probably a forgotten setting: say so, but start anyway."""
    settings = app.extensions["kami"].settings
    if _provider and settings.is_production and not settings.internal_token:
        log.warning("INTERNAL_TOKEN is empty: the attention endpoints are off (404)")


@bp.before_request
def _require_internal_token():
    token = current_app.extensions["kami"].settings.internal_token
    if not _provider or not token:
        abort(404)
    if not same(request.headers.get("Authorization", ""), f"Bearer {token}"):
        return jsonify({"error": "Not authenticated"}), 401
    return None


@bp.get("")
def list_items():
    response = jsonify({"items": [item.to_dict() for item in _provider()], "generatedAt": _iso(datetime.now(UTC))})
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.post("/<item_id>/ack")
def ack_item(item_id: str):
    if not _ack_handler:
        abort(404)
    if any(item.id == item_id and not item.ackable for item in _provider()):
        abort(400, "This item cannot be acknowledged")
    if not _ack_handler(item_id):
        abort(404)
    return "", 204


def init(app: Flask) -> None:
    app.register_blueprint(bp)
