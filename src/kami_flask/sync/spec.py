"""What an app tells ``kami_flask.sync``: its synced entities and how mutations reach them."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# fullmatch, and [0-9] rather than \d: `$` admits a trailing newline and \d matches Unicode digits.
ID_RE = re.compile(r"[0-9a-f]{32}")
OP_ID_RE = re.compile(r"[0-9A-Fa-f-]{32,64}")
ID_MESSAGE = "id must be a 32-character hex id"

# 4xx errors of the app's services are the op's fault and will never heal: reject. Everything else stays `failed`.
DEFAULT_REJECTED_STATUS = {400: "invalid", 404: "gone", 409: "invalid"}

ALL_OPS = ("create", "update", "delete")


class Rejected(Exception):  # noqa: N818 - a status, like the `rejected` result it produces
    """Raise from a validator or handler: the op is dropped by the client (status `rejected`, `error` = code)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def _own_id(obj: Any) -> str:
    return obj.id


@dataclass(frozen=True)
class Entity:
    """One synced entity (one model, one name on the wire).

    ``columns`` are the model attributes whose change reaches the phones; bookkeeping writes (updated_at, ...) are
    left out so they do not wake them. ``handler`` is the one mutation handler for this entity; without it the entity
    is read-only for clients (a mutation answers ``unsupported``).
    """

    name: str  # entity name on the wire and in tombstones, e.g. 'task'
    model: type
    columns: tuple[str, ...]
    feed_key: str  # key in the /changes response, e.g. 'tasks'
    serialize: Callable[[Any], dict[str, Any]]
    # handler(ctx, op, entity_id, fields) -> True to restamp the row afterwards (see Sync._remember), else False.
    handler: Callable[[Any, str, str, dict[str, Any]], bool] | None = None
    ops: tuple[str, ...] = ALL_OPS
    # validate(op, entity_id, fields): raise Rejected. Not called for deletes.
    validate: Callable[[str, str, dict[str, Any]], None] | None = None
    id_re: re.Pattern[str] = ID_RE
    id_message: str = ID_MESSAGE
    id_of: Callable[[Any], str] = _own_id  # the id the phone knows the row by
    # Child -> (parent model, the child's foreign-key attribute). A child deleted in the same flush as its parent
    # gets no tombstone of its own: the parent's covers it (or its tombstone_children).
    parent: tuple[type, str] | None = None
    # (child table, foreign key column, child entity name): deleting this entity also tombstones those rows
    # (for children the phone keeps as separate rows and that cannot be loaded, e.g. lazy='dynamic').
    tombstone_children: tuple[str, str, str] | None = None
    # To-many collection attributes whose added/removed members count as a change of this entity.
    collections: tuple[str, ...] = ()
    # Restamp lookup for ids that are not the `id` column: id -> (SQL where clause, params).
    restamp_where: Callable[[str], tuple[str, dict[str, Any]]] | None = None
    # If no row matches on restamp, restamp the tombstone of that id instead (the phone skipped that one too).
    restamp_tombstone: bool = False
    read_only_message: str = ""


@dataclass(frozen=True)
class StampsParent:
    """A model that is not an entity but belongs to one: a change restamps the parent (e.g. routine steps)."""

    model: type
    parent_attr: str  # attribute on the child that holds the parent object
    columns: tuple[str, ...]  # own columns whose change counts


@dataclass(frozen=True)
class SyncSpec:
    entities: tuple[Entity, ...]
    stamps_parents: tuple[StampsParent, ...] = ()
    # Built once per /mutations request and handed to every handler (e.g. service objects).
    context: Callable[[], Any] = field(default=lambda: None)
    rejected_status: dict[int, str] = field(default_factory=lambda: dict(DEFAULT_REJECTED_STATUS))
    max_mutations: int = 200
    default_limit: int = 500
    max_limit: int = 1000
    tombstone_days: int = 90
    applied_ops_days: int = 30
    prune_chunk: int = 500

    def __post_init__(self) -> None:
        names = [e.name for e in self.entities]
        keys = [e.feed_key for e in self.entities]
        models = [e.model for e in self.entities]
        if len(set(names)) != len(names) or len(set(keys)) != len(keys) or len(set(models)) != len(models):
            raise ValueError("sync spec: entity names, feed keys and models must be unique")
        if "tombstones" in keys:
            raise ValueError("sync spec: 'tombstones' is reserved as a feed key")
        for entity in self.entities:
            for op in entity.ops:
                if op not in ALL_OPS:
                    raise ValueError(f"sync spec: {entity.name}: unknown op {op!r}")
