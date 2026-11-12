"""Bookkeeping tables: the sequence counter, tombstones and applied ops.

The table and column names are fixed (``sync_state``, ``tombstones``, ``applied_ops``) so an app that had them
before the extraction needs no migration. The library owns no metadata: the models are built on the app's own
``db.Model`` the first time they are asked for, and cached per ``db``.
"""

from __future__ import annotations

import weakref
from datetime import UTC, datetime
from typing import Any, NamedTuple


def utcnow() -> datetime:
    """Naive UTC, like the app columns."""
    return datetime.now(UTC).replace(tzinfo=None)


class SyncModels(NamedTuple):
    SyncState: type
    Tombstone: type
    AppliedOp: type


_cache: weakref.WeakKeyDictionary[Any, SyncModels] = weakref.WeakKeyDictionary()


def models_for(db: Any) -> SyncModels:
    """The bookkeeping models on this Flask-SQLAlchemy ``db`` (created once)."""
    if db in _cache:
        return _cache[db]

    class SyncState(db.Model):
        """Single row (id=1): the last change_seq handed out, and the oldest cursor still answerable."""

        __tablename__ = "sync_state"

        id = db.Column(db.Integer, primary_key=True)
        last_seq = db.Column(db.Integer, nullable=False, default=0)
        # Cursors below this miss pruned tombstones: /changes answers 410 and the client resyncs from 0.
        pruned_below = db.Column(db.Integer, nullable=False, default=0)

    class Tombstone(db.Model):
        """A hard-deleted row, so clients can drop their copy."""

        __tablename__ = "tombstones"

        entity = db.Column(db.String(16), primary_key=True)
        id = db.Column(db.String(64), primary_key=True)
        change_seq = db.Column(db.Integer, nullable=False, index=True)
        deleted_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    class AppliedOp(db.Model):
        """opIds already applied by the mutation endpoint (replays answer `duplicate`)."""

        __tablename__ = "applied_ops"

        op_id = db.Column(db.String(64), primary_key=True)
        applied_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    _cache[db] = SyncModels(SyncState, Tombstone, AppliedOp)
    return _cache[db]
