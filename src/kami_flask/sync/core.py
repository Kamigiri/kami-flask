"""The sync engine: change stamping, the change feed, the mutation queue, prune and force_resync.

Everything here is SQLite-flavoured raw SQL on the bookkeeping tables (RETURNING, ON CONFLICT, INSERT OR IGNORE).
Never use bulk ``query.update()`` / ``query.delete()`` on a synced model: it skips the flush listener.
"""

from __future__ import annotations

import logging
import weakref
from datetime import timedelta
from typing import Any

from flask import Flask, jsonify
from sqlalchemy import event, inspect, text
from sqlalchemy.orm import Session

from .models import models_for, utcnow
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
        self.spec = spec
        self.models = models_for(db)
        self._by_model = {e.model: e for e in spec.entities}
        self._by_name = {e.name: e for e in spec.entities}
        self._stamps_parent = {s.model: s for s in spec.stamps_parents}
        # Models whose delete must be known before the others are looked at (parents of other entities).
        self._parent_models = {e.parent[0] for e in spec.entities if e.parent} | {
            e.model for e in spec.entities if e.tombstone_children
        }
        event.listen(db.session, "before_flush", self._before_flush)

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

    def _before_flush(self, session: Session, _flush_context, _instances) -> None:
        conn = None
        new, dirty, deleted = list(session.new), list(session.dirty), list(session.deleted)

        # Collect first: session.deleted is unordered, and a parent being deleted must not be restamped.
        gone: dict[type, set[str]] = {}
        for obj in deleted:
            if type(obj) in self._parent_models:
                gone.setdefault(type(obj), set()).add(obj.id)

        stamped: set[int] = set()

        def stamp(obj: object) -> None:
            if id(obj) not in stamped:
                stamped.add(id(obj))
                obj.change_seq = self.next_seq(session)

        def stamp_parent(child: object) -> None:
            parent = getattr(child, self._stamps_parent[type(child)].parent_attr, None)
            if parent is not None and parent.id not in gone.get(type(parent), ()) and parent not in session.deleted:
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
                    if obj.id not in gone.get(model, ()):
                        stamp(obj)
            elif model in self._stamps_parent and session.is_modified(obj):
                if _changed(obj, self._stamps_parent[model].columns):
                    stamp_parent(obj)

        for obj in deleted:
            entity = self._by_model.get(type(obj))
            if entity is None:
                continue
            if entity.parent and getattr(obj, entity.parent[1]) in gone.get(entity.parent[0], ()):
                continue  # covered by the parent's tombstone
            conn = conn or session.connection()
            self._tombstone(conn, entity.name, entity.id_of(obj), self.next_seq(session))
            if entity.tombstone_children:
                table, fk, name = entity.tombstone_children
                rows = conn.execute(text(f"SELECT id FROM {table} WHERE {fk} = :p"), {"p": obj.id}).scalars().all()
                for child_id in rows:
                    self._tombstone(conn, name, child_id, self.next_seq(session))

    # ---- prune / force_resync ---------------------------------------------------------------------------------

    def prune(self, session: Session) -> dict[str, int]:
        """Drop old tombstones and old applied ops, in chunks. pruned_below rises to the highest pruned tombstone
        seq in the same transaction: a cursor below it may have missed a delete (410), a cursor at or above it has
        not."""
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

    def force_resync(self, session: Session) -> int:
        """After a restore (or a rollback to an image without stamping): every client must resync from 0.

        A restored database may be *behind* the clients' cursors, so the counter jumps far ahead and every cursor
        handed out before is below pruned_below (410 -> full resync); a full pull ends exactly at it.
        Returns pruned_below.
        """
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
        state = self.db.session.get(self.models.SyncState, 1)
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
                model.query.filter(model.change_seq > since, model.change_seq <= last_seq)
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
        self.db.session.rollback()
        results.append({"opId": op_id, "status": "rejected", "error": code, "message": message})
        if op_id and OP_ID_RE.fullmatch(op_id):
            self._remember(op_id)

    def _failed(self, results: list[dict[str, Any]], op_id: str | None, error: Exception) -> None:
        self.db.session.rollback()
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
                if self.db.session.get(self.models.AppliedOp, op_id):
                    results.append({"opId": op_id, "status": "duplicate"})
                    continue
                restamp = entity.handler(ctx, op, entity_id, fields)
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
        session = self.db.session
        if touch:
            entity, ident = touch
            seq = self.next_seq(session)
            table = entity.model.__tablename__
            if entity.restamp_where:
                where, params = entity.restamp_where(ident)
            else:
                where, params = "id = :i", {"i": ident}
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
