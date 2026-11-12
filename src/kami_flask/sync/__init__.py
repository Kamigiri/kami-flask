"""Offline-first sync server for apps with Android (or other) clients: ``pip install "kami-flask[sync]"``.

An app describes its synced entities in a :class:`SyncSpec` and calls :func:`register` with its own Flask-SQLAlchemy
``db``. The returned :class:`Sync` stamps ``change_seq`` on every flush, writes tombstones, serves the change feed
(``changes``) and the mutation queue (``apply_mutations``) and offers ``prune`` and ``force_resync``. The app keeps
its routes; see the README section "kami_flask.sync".
"""

from .core import StaleCursor, Sync, SyncError, register
from .models import SyncModels, models_for, utcnow
from .spec import DEFAULT_REJECTED_STATUS, ID_RE, OP_ID_RE, Entity, Rejected, StampsParent, SyncSpec

__all__ = [
    "DEFAULT_REJECTED_STATUS",
    "ID_RE",
    "OP_ID_RE",
    "Entity",
    "Rejected",
    "StaleCursor",
    "StampsParent",
    "Sync",
    "SyncError",
    "SyncModels",
    "SyncSpec",
    "models_for",
    "register",
    "utcnow",
]
