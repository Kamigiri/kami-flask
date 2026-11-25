"""The sync engine: change stamping, the change feed, the mutation queue, prune and force_resync.

Two ways in: ``register(db, spec)`` for Flask-SQLAlchemy and ``register_sqlalchemy(base, session, spec, bind=...)``
for plain SQLAlchemy.

Everything here is SQLite-flavoured raw SQL on the bookkeeping tables (RETURNING, ON CONFLICT, INSERT OR IGNORE).
Never use bulk ``query.update()`` / ``query.delete()`` on a synced model: it skips the flush listener.
"""

from __future__ import annotations

import logging
import weakref
from collections.abc import Callable, Iterable
from datetime import timedelta
from typing import Any

from flask import Flask, jsonify
from sqlalchemy import event, inspect, text
from sqlalchemy.orm import MANYTOONE, Session, sessionmaker
from sqlalchemy.orm.attributes import set_committed_value

from .models import PLAIN_MODELS, models_for, plain_metadata, utcnow
from .spec import OP_ID_RE, Entity, Rejected, SyncSpec

logger = logging.getLogger("kami.sync")

RESYNC_JUMP = 1_000_000


class SyncError(Exception):
    """A request-level problem with an HTTP status (400 bad arguments, 410 stale cursor)."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class StaleCursor(SyncError):
    """The cursor is older than the pruned tombstones (or newer than the server): the client resyncs from 0."""

    def __init__(self) -> None:
        super().__init__("Cursor is stale, resync from 0", status_code=410)


_registry: weakref.WeakKeyDictionary[Any, Sync] = weakref.WeakKeyDictionary()


def register(db: Any, spec: SyncSpec) -> Sync:
    """Wire sync to the app's ``db`` (Flask-SQLAlchemy): builds the bookkeeping models and attaches the stamping
    listener. Idempotent per ``db`` (create_app runs many times in tests; the session class is shared)."""
    if db in _registry:
        return _registry[db]
    sync = Sync(db, spec)
    _registry[db] = sync
    return sync


_plain_syncs: weakref.WeakKeyDictionary[Any, PlainSync] = weakref.WeakKeyDictionary()


def register_sqlalchemy(
    base: Any,
    session: Callable[[], Session],
    spec: SyncSpec,
    *,
    bind: Any,
    run_after: Iterable[Callable[..., Any]] = (),
) -> PlainSync:
    """Wire sync to a plain-SQLAlchemy app (no Flask-SQLAlchemy).

    ``base`` is the app's declarative base. ``session`` returns the session that ``changes``, ``apply_mutations``,
    ``prune`` and ``force_resync`` work on (``lambda: g.db``, or a ``scoped_session``). Stamping is attached to the
    ``Session`` class, so every session stamps, however it was built. The bookkeeping tables are created on ``bind``
    now, and again by any later ``base.metadata.create_all``.

    ``run_after``: the app's own ``before_flush`` listeners on ``Session`` that change synced columns. Listeners
    fire in registration order, so each must already be registered (ValueError otherwise).

    Idempotent per ``base``: a later call keeps the first spec and listener, takes the new ``session`` and creates
    the tables on the new ``bind``.
    """
    if isinstance(session, sessionmaker) or not callable(session):
        raise TypeError("session must be a callable returning the current session, not a sessionmaker or a Session")
    sync = _plain_syncs.get(base)
    if sync is None:
        for listener in run_after:
            if not event.contains(Session, "before_flush", listener):
                name = getattr(listener, "__name__", listener)
                raise ValueError(f"sync: {name} must be a before_flush listener on Session before register_sqlalchemy")
        sync = _plain_syncs[base] = PlainSync(session, spec)
        event.listen(base.metadata, "after_create", sync._after_create)
    sync._get_session = session
    sync.create_tables(bind)
    return sync


def _pk(obj: object) -> Any:
    """The internal identity (what foreign keys point at), which need not be the wire id."""
    values = inspect(obj).mapper.primary_key_from_instance(obj)
    return values[0] if len(values) == 1 else tuple(values)


def _column(model: type, attr: str) -> str:
    """The column name behind a mapped attribute."""
    columns = inspect(model).columns
    if attr not in columns:
        raise ValueError(f"sync spec: {model.__name__}.{attr} is not a mapped column")
    return columns[attr].name


def _parent_of(session: Session, child: object, attr: str) -> Any:
    """The parent object. A pending child that only has its foreign key set loads nothing through the relationship,
    so the parent is looked up by that key."""
    parent = getattr(child, attr, None)
    if parent is not None:
        return parent
    relation = inspect(type(child)).relationships.get(attr)
    if relation is None or relation.direction is not MANYTOONE or len(relation.local_remote_pairs) != 1:
        return None
    local, remote = relation.local_remote_pairs[0]
    value = getattr(child, relation.parent.get_property_by_column(local).key)
    if value is None or list(relation.mapper.primary_key) != [remote]:
        return None
    with session.no_autoflush:
        return session.get(relation.mapper.class_, value)


def _int_arg(raw: str | None, name: str, default: int) -> int:
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SyncError(f"{name} must be a non-negative integer") from None
    if value < 0:
        raise SyncError(f"{name} must be a non-negative integer")
    return value


def _changed(obj: object, columns: tuple[str, ...]) -> bool:
    """True if any synced attribute really changed (re-assigning the same value is not a change)."""
    attrs = inspect(obj).attrs
    for name in columns:
        history = attrs[name].history
        if history.added and list(history.added) != list(history.deleted):
            return True
    return False


def _collections_changed(obj: object, names: tuple[str, ...]) -> bool:
    attrs = inspect(obj).attrs
    for name in names:
        history = attrs[name].history
        if history.added or history.deleted:
            return True
    return False


class Sync:
    def __init__(self, db: Any, spec: SyncSpec) -> None:
        self.db = db
        self.models = models_for(db)
        self._setup(spec)
        event.listen(db.session, "before_flush", self._before_flush)

    def _setup(self, spec: SyncSpec) -> None:
        self.spec = spec
        self._by_model = {e.model: e for e in spec.entities}
        self._by_name = {e.name: e for e in spec.entities}
        self._stamps_parent = {s.model: s for s in spec.stamps_parents}
        # Models whose delete must be known before the others are looked at (parents of other entities).
        self._parent_models = {e.parent[0] for e in spec.entities if e.parent} | {
            e.model for e in spec.entities if e.tombstone_children
        }
        for e in spec.entities:  # a mistyped id_attr should fail at startup, not on the first delete
            if e.id_attr != "id":
                _column(e.model, e.id_attr)

    def _session(self) -> Session:
        return self.db.session

    def _query(self, model: type):
        return model.query

    def init_app(self, app: Flask) -> None:
        """JSON answers for SyncError (400 / 410), next to the app's own error handlers."""

        @app.errorhandler(SyncError)
        def _sync_error(error: SyncError):
            response = jsonify({"error": error.message})
            response.status_code = error.status_code
            return response

    # ---- stamping ---------------------------------------------------------------------------------------------

    def next_seq(self, session: Session) -> int:
        """Atomically take the next sequence number inside the session's transaction (other threads write too)."""
        conn = session.connection()
        seq = conn.execute(
            text("UPDATE sync_state SET last_seq = last_seq + 1 WHERE id = 1 RETURNING last_seq")
        ).scalar()
        if seq is None:
            conn.execute(text("INSERT INTO sync_state (id, last_seq, pruned_below) VALUES (1, 1, 0)"))
            seq = 1
        return seq

    @staticmethod
    def _tombstone(conn, entity: str, entity_id: str, seq: int) -> None:
        conn.execute(
            text(
                "INSERT INTO tombstones (entity, id, change_seq, deleted_at) VALUES (:e, :i, :s, :d) "
                "ON CONFLICT (entity, id) DO UPDATE SET change_seq = excluded.change_seq, "
                "deleted_at = excluded.deleted_at"
            ),
            {"e": entity, "i": entity_id, "s": seq, "d": utcnow()},
        )

    def stamp(self, session: Session, obj: object) -> int | None:
        """Give a synced row a fresh seq right now, for writes the flush listener cannot see (bulk updates, core
        statements). Runs in the session's transaction; the caller commits. A ``stamps_parents`` child stamps its
        parent. Returns the seq, or None if there was no persistent row to stamp (a pending object is left to the
        flush). Deletes must go through the ORM, so that they write a tombstone."""
        model = type(obj)
        if model in self._stamps_parent:
            parent = _parent_of(session, obj, self._stamps_parent[model].parent_attr)
            return None if parent is None else self.stamp(session, parent)
        if model not in self._by_model:
            raise ValueError(f"sync: {model.__name__} is not a synced model")
        state = inspect(obj)
        if state.identity is None:
            return None
        seq = self.next_seq(session)
        keys = {f"k{i}": value for i, value in enumerate(state.identity)}
        where = " AND ".join(f"{column.name} = :k{i}" for i, column in enumerate(state.mapper.primary_key))
        # Textual SQL: no flush listener, and no `onupdate` column defaults (updated_at stays).
        session.connection().execute(
            text(f"UPDATE {model.__tablename__} SET change_seq = :s WHERE {where}"), {"s": seq, **keys}
        )
        set_committed_value(obj, "change_seq", seq)
        return seq

    def _before_flush(self, session: Session, _flush_context, _instances) -> None:
        conn = None
        new, dirty, deleted = list(session.new), list(session.dirty), list(session.deleted)

        # Collect first: session.deleted is unordered, and a parent being deleted must not be restamped.
        # Primary keys, not wire ids: they are compared with foreign keys.
        gone: dict[type, set[Any]] = {}
        for obj in deleted:
            if type(obj) in self._parent_models:
                gone.setdefault(type(obj), set()).add(_pk(obj))

        def is_gone(obj: object) -> bool:
            return type(obj) in gone and _pk(obj) in gone[type(obj)]

        stamped: set[int] = set()

        def stamp(obj: object) -> None:
            if id(obj) not in stamped:
                stamped.add(id(obj))
                obj.change_seq = self.next_seq(session)

        def stamp_parent(child: object) -> None:
            parent = _parent_of(session, child, self._stamps_parent[type(child)].parent_attr)
            if parent is not None and not is_gone(parent) and parent not in session.deleted:
                stamp(parent)

        for obj in new:
            model = type(obj)
            entity = self._by_model.get(model)
            if entity:
                stamp(obj)
                conn = conn or session.connection()
                ident = entity.id_of(obj)
                if ident:  # re-creating a deleted id brings it back
                    conn.execute(
                        text("DELETE FROM tombstones WHERE entity = :e AND id = :i"), {"e": entity.name, "i": ident}
                    )
            elif model in self._stamps_parent:
                stamp_parent(obj)

        for obj in dirty:
            model = type(obj)
            entity = self._by_model.get(model)
            if entity and session.is_modified(obj):
                if obj in session.deleted:
                    continue
                if _changed(obj, entity.columns) or _collections_changed(obj, entity.collections):
                    if not is_gone(obj):
                        stamp(obj)
            elif model in self._stamps_parent and session.is_modified(obj):
                if _changed(obj, self._stamps_parent[model].columns):
                    stamp_parent(obj)

        for obj in deleted:
            entity = self._by_model.get(type(obj))
            if entity is None:
                if type(obj) in self._stamps_parent:  # a removed child changes its parent
                    stamp_parent(obj)
                continue
            if entity.parent and getattr(obj, entity.parent[1]) in gone.get(entity.parent[0], ()):
                continue  # covered by the parent's tombstone
            conn = conn or session.connection()
            ident = entity.id_of(obj)
            if ident:  # a row without a wire id never reached a client
                self._tombstone(conn, entity.name, ident, self.next_seq(session))
            if entity.tombstone_children:
                table, fk, name = entity.tombstone_children
                child = self._by_name.get(name)
                column = _column(child.model, child.id_attr) if child and child.id_attr != "id" else "id"
                rows = conn.execute(text(f"SELECT {column} FROM {table} WHERE {fk} = :p"), {"p": _pk(obj)})
                for child_id in rows.scalars().all():
                    if child_id:
                        self._tombstone(conn, name, child_id, self.next_seq(session))

    # ---- prune / force_resync ---------------------------------------------------------------------------------

    def prune(self, session: Session | None = None) -> dict[str, int]:
        """Drop old tombstones and old applied ops, in chunks. pruned_below rises to the highest pruned tombstone
        seq in the same transaction: a cursor below it may have missed a delete (410), a cursor at or above it has
        not. Commits."""
        session = session if session is not None else self._session()
        spec = self.spec
        conn = session.connection()
        tomb_cutoff = utcnow() - timedelta(days=spec.tombstone_days)
        ops_cutoff = utcnow() - timedelta(days=spec.applied_ops_days)
        removed_tombstones = removed_ops = 0
        while True:
            rows = conn.execute(
                text(
                    "SELECT entity, id, change_seq FROM tombstones WHERE deleted_at < :c "
                    "ORDER BY change_seq LIMIT :n"
                ),
                {"c": tomb_cutoff, "n": spec.prune_chunk},
            ).all()
            if not rows:
                break
            highest = rows[-1].change_seq
            raised = conn.execute(
                text("UPDATE sync_state SET pruned_below = MAX(pruned_below, :h) WHERE id = 1"), {"h": highest}
            ).rowcount
            if raised == 0:
                conn.execute(
                    text("INSERT INTO sync_state (id, last_seq, pruned_below) VALUES (1, :h, :h)"), {"h": highest}
                )
            for row in rows:
                conn.execute(
                    text("DELETE FROM tombstones WHERE entity = :e AND id = :i"), {"e": row.entity, "i": row.id}
                )
            session.commit()
            conn = session.connection()
            removed_tombstones += len(rows)
        while True:
            result = conn.execute(
                text(
                    "DELETE FROM applied_ops WHERE op_id IN "
                    "(SELECT op_id FROM applied_ops WHERE applied_at < :c LIMIT :n)"
                ),
                {"c": ops_cutoff, "n": spec.prune_chunk},
            )
            session.commit()
            conn = session.connection()
            removed_ops += result.rowcount
            if result.rowcount < spec.prune_chunk:
                break
        if removed_tombstones or removed_ops:
            logger.info(f"Sync prune: {removed_tombstones} tombstones, {removed_ops} applied ops")
        return {"tombstones": removed_tombstones, "appliedOps": removed_ops}

    def force_resync(self, session: Session | None = None) -> int:
        """After a restore (or a rollback to an image without stamping): every client must resync from 0.

        A restored database may be *behind* the clients' cursors, so the counter jumps far ahead and every cursor
        handed out before is below pruned_below (410 -> full resync); a full pull ends exactly at it.
        Returns pruned_below. Commits.
        """
        session = session if session is not None else self._session()
        self.next_seq(session)  # makes sure the row exists
        session.execute(
            text("UPDATE sync_state SET last_seq = last_seq + :j, pruned_below = last_seq + :j WHERE id = 1"),
            {"j": RESYNC_JUMP},
        )
        floor = session.execute(text("SELECT pruned_below FROM sync_state WHERE id = 1")).scalar()
        session.commit()
        return floor

    # ---- change feed ------------------------------------------------------------------------------------------

    def _state(self) -> tuple[int, int]:
        state = self._session().get(self.models.SyncState, 1)
        return (state.last_seq, state.pruned_below) if state else (0, 0)

    def changes(self, since_raw: str | None, limit_raw: str | None) -> dict[str, Any]:
        """Rows changed after `since`, every entity and the tombstones merged in seq order, cut at `limit`.

        The cursor means "the client has everything up to here". The reads are capped at the `last_seq` read
        first: SQLite has one writer and stamps in commit order, so every seq up to it is committed and none can
        appear later behind the cursor. The last page returns `last_seq` itself, so a cursor never ends below
        `pruned_below` (which would 410 forever after a prune or force_resync).
        """
        spec = self.spec
        since = _int_arg(since_raw, "since", 0)
        limit = min(max(_int_arg(limit_raw, "limit", spec.default_limit), 1), spec.max_limit)
        last_seq, pruned_below = self._state()
        if since and (since < pruned_below or since > last_seq):
            raise StaleCursor()

        def window(model):
            return (
                self._query(model)
                .filter(model.change_seq > since, model.change_seq <= last_seq)
                .order_by(model.change_seq)
                .limit(limit + 1)
                .all()
            )

        tables = [(e.model, e) for e in spec.entities] + [(self.models.Tombstone, None)]
        merged = sorted(
            [(row.change_seq, entity, row) for model, entity in tables for row in window(model)],
            key=lambda item: item[0],
        )
        page = merged[:limit]
        has_more = len(merged) > limit

        out: dict[str, Any] = {
            "cursor": page[-1][0] if has_more else max(last_seq, since),
            "hasMore": has_more,
        }
        for entity in spec.entities:
            out[entity.feed_key] = [entity.serialize(row) for _, kind, row in page if kind is entity]
        out["tombstones"] = [{"entity": row.entity, "id": row.id} for _, kind, row in page if kind is None]
        return out

    # ---- mutation queue ---------------------------------------------------------------------------------------

    def _validate(self, m: Any) -> tuple[str, Entity, str, str, dict[str, Any]]:
        """Shape checks for one op. Returns (opId, entity, op, id, fields); raises Rejected."""
        if not isinstance(m, dict):
            raise Rejected("invalid", "mutation must be an object")
        op_id, name, op, entity_id = m.get("opId"), m.get("entity"), m.get("op"), m.get("id")
        fields = m.get("fields") if m.get("fields") is not None else {}
        if not isinstance(op_id, str) or not OP_ID_RE.fullmatch(op_id):
            raise Rejected("invalid", "opId must be a UUID or 32-character hex")
        entity = self._by_name.get(name) if isinstance(name, str) else None
        if entity is not None and entity.handler is None:
            raise Rejected("unsupported", entity.read_only_message or f"{name} is read-only for clients")
        if entity is None:
            writable = ", ".join(e.name for e in self.spec.entities if e.handler is not None)
            raise Rejected("invalid", f"entity must be one of: {writable}")
        if op not in entity.ops:
            raise Rejected("invalid", f"op must be {', '.join(entity.ops)}")
        if not isinstance(entity_id, str) or not entity.id_re.fullmatch(entity_id):
            raise Rejected("invalid", entity.id_message)
        if not isinstance(fields, dict):
            raise Rejected("invalid", "fields must be an object")
        if op != "delete" and entity.validate:
            entity.validate(op, entity_id, fields)
        return op_id, entity, op, entity_id, fields

    def _rejected(self, results: list[dict[str, Any]], op_id: str | None, code: str, message: str) -> None:
        self._session().rollback()
        results.append({"opId": op_id, "status": "rejected", "error": code, "message": message})
        if op_id and OP_ID_RE.fullmatch(op_id):
            self._remember(op_id)

    def _failed(self, results: list[dict[str, Any]], op_id: str | None, error: Exception) -> None:
        self._session().rollback()
        # The type only: SQL parameters and upstream errors can hold titles, notes and tokens.
        logger.warning(f"Mutation {op_id} failed, client will retry: {type(error).__name__}")
        results.append(
            {"opId": op_id, "status": "failed", "error": "retry", "message": "Temporary server problem, will retry"}
        )

    def apply_mutations(self, body: Any) -> dict[str, Any]:
        """Apply the ops in order. Results: applied | duplicate | rejected (dropped by the client) | failed.

        `failed` means a temporary problem (upstream unreachable, database busy): the batch stops there, the
        client keeps that op and every later one and retries. An exception with a 4xx `status_code` that is in
        ``spec.rejected_status`` (400, 404, 409 by default) is the op's fault and is `rejected`; anything else
        (401, 5xx, no status) stays `failed`.
        """
        spec = self.spec
        if not isinstance(body, dict) or not isinstance(body.get("mutations"), list):
            raise SyncError("Expected {mutations: [...]}")
        mutations = body["mutations"]
        if len(mutations) > spec.max_mutations:
            raise SyncError(f"At most {spec.max_mutations} mutations per request")

        ctx = spec.context()
        results: list[dict[str, Any]] = []
        for raw in mutations:
            op_id = raw.get("opId") if isinstance(raw, dict) and isinstance(raw.get("opId"), str) else None
            try:
                op_id, entity, op, entity_id, fields = self._validate(raw)
                if self._session().get(self.models.AppliedOp, op_id):
                    results.append({"opId": op_id, "status": "duplicate"})
                    continue
                restamp = entity.handler(ctx, op, entity_id, fields)
                # Handlers need not commit or flush: flush errors belong to this op, and the restamp below is
                # raw SQL that must see the handler's rows (sessions with autoflush off).
                self._session().flush()
            except Rejected as r:
                self._rejected(results, op_id, r.code, r.message)
                continue
            except Exception as e:
                status = getattr(e, "status_code", None)
                code = spec.rejected_status.get(status) if isinstance(status, int) else None
                if code is None:
                    self._failed(results, op_id, e)
                    break
                self._rejected(results, op_id, code, getattr(e, "message", None) or str(e))
                continue
            self._remember(op_id, (entity, entity_id) if restamp else None)
            results.append({"opId": op_id, "status": "applied"})

        last_seq, _ = self._state()
        return {"results": results, "cursor": last_seq}

    def _remember(self, op_id: str | None, touch: tuple[Entity, str] | None = None) -> None:
        """Record the op as applied. `touch`: restamp that row, even if the op changed nothing. The client skipped
        the server version of a row while its op was queued; without a fresh seq that version would never come
        back. An entity that no longer exists restamps its tombstone instead, if `restamp_tombstone` is set."""
        if not op_id:
            return
        session = self._session()
        if touch:
            entity, ident = touch
            seq = self.next_seq(session)
            table = entity.model.__tablename__
            if entity.restamp_where:
                where, params = entity.restamp_where(ident)
            else:
                column = "id" if entity.id_attr == "id" else _column(entity.model, entity.id_attr)
                where, params = f"{column} = :i", {"i": ident}
            hit = session.execute(
                text(f"UPDATE {table} SET change_seq = :s WHERE {where}"), {"s": seq, **params}
            ).rowcount
            if not hit and entity.restamp_tombstone:
                session.execute(
                    text("UPDATE tombstones SET change_seq = :s WHERE entity = :e AND id = :i"),
                    {"s": seq, "e": entity.name, "i": ident},
                )
        session.execute(
            text("INSERT OR IGNORE INTO applied_ops (op_id, applied_at) VALUES (:o, :t)"),
            {"o": op_id, "t": utcnow()},
        )
        session.commit()


class PlainSync(Sync):
    """``Sync`` for plain SQLAlchemy: built by ``register_sqlalchemy``."""

    def __init__(self, session: Callable[[], Session], spec: SyncSpec) -> None:
        self.db = None
        self.models = PLAIN_MODELS
        self._get_session = session
        self._setup(spec)
        event.listen(Session, "before_flush", self._before_flush)

    def _session(self) -> Session:
        return self._get_session()

    def _query(self, model: type):
        return self._session().query(model)

    def create_tables(self, bind: Any) -> None:
        """Create the bookkeeping tables that are missing."""
        plain_metadata.create_all(bind)

    def _after_create(self, _target, connection, **_kw) -> None:
        plain_metadata.create_all(connection)
