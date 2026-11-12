# kami-flask

The shared Flask backend framework for the Kamigiri apps. One `KamiApp` gives an app:

- **Settings** from the environment, with `APP_ENV=production` refusing default secrets
- **Auth**: single-user session login (`/api/auth/login|logout|me`), optional Bearer
  `API_TOKEN`, login throttle, `X-Requested-With` required for cookie-authed writes
- **Gateway auth** (optional): behind a reverse proxy that authenticates users (Authelia and
  nginx `auth_request`), set `GATEWAY_SECRET`. The guard then trusts `Remote-User` only on
  requests that also carry the same secret in `X-Kami-Gateway` (constant-time compare); the
  proxy must set both headers itself and strip client-sent copies. Order: gateway, Bearer,
  session, so the local login keeps working for clients that bypass the proxy.
  `/api/auth/me` reports `{"auth": "gateway", "logout_url": GATEWAY_LOGOUT_URL}`; writes
  still need `X-Requested-With`. Leave `GATEWAY_SECRET` empty to turn it off.
- **Platform contract**: `/api/health` (liveness), `/api/ready` (DB check), `/api/version`
  (version + git revision from the image)
- **Logs**: one JSON line per request on stdout (`request_id`, route, status, duration, user)
- **Metrics**: Prometheus on `METRICS_PORT` (not proxied by nginx), RED metrics per route,
  `app_info`, errors, and background jobs via `job("name")`
- **Errors** as `{"error": msg}` (short names for werkzeug defaults, e.g. `"Not Found"`), **SPA serving** (WhiteNoise + index.html fallback)
- **pytest plugin**: `client` (logged in), `anon_client`, `bearer_client`, `gateway_client`

```python
from dataclasses import dataclass
from kami_flask import KamiApp, Settings, env, job

@dataclass
class TasksSettings(Settings):
    google_sync_enabled: bool = env("GOOGLE_SYNC_ENABLED", False)

    def flask_config(self):
        return {"GOOGLE_SYNC_ENABLED": self.google_sync_enabled}

def create_app():
    kami = KamiApp("tasks", TasksSettings(), version="2.0.0", import_name=__name__,
                   ready_check=lambda: db.session.execute(text("select 1")))
    app = kami.flask
    app.register_blueprint(routes.bp)
    kami.serve_spa()          # last
    return app

@job("google_sync")
def run_sync(): ...
```

## kami_flask.sync (optional, `kami-flask[sync]`)

The offline-first sync server for apps with an Android client (extracted from tasks in 0.3). SQLite only, and
SQLAlchemy comes in through the extra: `kami-flask[sync] @ https://github.com/Kamigiri/kami-flask/archive/refs/tags/v0.3.0.tar.gz`.

The app describes its synced entities and passes its own Flask-SQLAlchemy `db`; the library owns no metadata.
Every synced model needs an indexed integer `change_seq` column.

```python
from kami_flask.sync import Entity, Rejected, StampsParent, SyncSpec, register

SPEC = SyncSpec(
    entities=(
        Entity("routine", Routine, ("name", "structure"), "routines", RoutineSchema.to_dict,
               handler=handle_routine, validate=check_routine, tombstone_children=None),
        Entity("session", Session, ("local_date", "finished_at"), "sessions", SessionSchema.to_dict,
               handler=handle_session),
        Entity("workoutSet", WorkoutSet, ("reps", "seconds"), "workoutSets", SetSchema.to_dict,
               handler=handle_set, parent=(Session, "session_id")),  # a session delete writes one tombstone
    ),
    stamps_parents=(StampsParent(RoutineStep, "routine", ("title", "position")),),  # optional
    context=lambda: MyServices(),  # built once per /mutations request, handed to every handler
)
sync = register(db, SPEC)   # attaches the flush listener; idempotent per db
sync.init_app(app)          # JSON 400 / 410 for SyncError and StaleCursor

@bp.get("/changes")
def changes():
    return jsonify(sync.changes(request.args.get("since"), request.args.get("limit")))

@bp.post("/mutations")
def mutations():
    return jsonify(sync.apply_mutations(request.get_json(silent=True)))
```

- **Entity**: `name` (wire and tombstones), `model`, `columns` (only these count as a change; bookkeeping writes
  do not wake the phones), `feed_key` and `serialize` for `/changes`. Optional: `handler` (the single mutation
  handler of the entity, a table entry rather than an if-chain; without one the entity is read-only and answers
  `unsupported`), `validate(op, id, fields)`, `ops`, `id_re`/`id_message`/`id_of` (ids that are not the `id`
  column), `parent` (`(ParentModel, fk_attr)`: a child deleted together with its parent gets no tombstone of
  its own), `tombstone_children` (`(table, fk column, entity name)`: the parent's delete tombstones children
  that were never loaded), `collections`, `restamp_where`, `restamp_tombstone`.
- **Handlers** `handler(ctx, op, entity_id, fields) -> bool` run in the request transaction; return `True` to
  restamp the row after the op (even a no-op update: the client skipped that row's server version while its op
  was queued). Raise `Rejected(code, message)` to drop the op. An exception with a 4xx `status_code`
  (`spec.rejected_status`: 400, 404, 409) is rejected as well; anything else is `failed`, which stops the
  batch so the client retries (the message is never echoed).
- **Feed**: entities and tombstones merged in `change_seq` order, `limit` (default 500, max 1000), the cursor
  is capped at the `last_seq` read first, `StaleCursor` (410) when the cursor is below `pruned_below` or ahead
  of the server.
- **Maintenance**: `sync.prune(db.session)` (tombstones after 90 days, applied ops after 30; run it daily from a
  `job()`), `sync.force_resync(db.session)` (after a restore: every client resyncs from 0).
- Table names are fixed: `sync_state`, `tombstones`, `applied_ops`. Never use bulk `query.update()` /
  `query.delete()` on a synced model; it skips the flush listener.

Install (apps pin a release tag; no git needed in Docker):

```
kami-flask @ https://github.com/Kamigiri/kami-flask/archive/refs/tags/v0.1.0.tar.gz
```

Constraints: one gunicorn worker per app (metrics and the login throttle live in the
process; use threads for concurrency).

Development: `python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"`,
then `python -m pytest` and `ruff check .`. Release steps: `CHANGELOG.md`.
Design notes live in the (private) platform repo.
