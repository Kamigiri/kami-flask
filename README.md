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

## kami_flask.claude_runner

Client for the platform's internal Claude runner: one service that runs headless Claude Code **without tools** and returns
output matching a JSON schema. Standard library only.

```python
from kami_flask.claude_runner import FakeRunner, RunnerClient, image_block, text_block

runner = RunnerClient(env_url, env_token, client="tasks")    # CLAUDE_RUNNER_URL, CLAUDE_RUNNER_TOKEN
result = runner.run(model="claude-sonnet-5-5", system=PROMPT, schema=SCHEMA,
                    content=[image_block(jpeg, "image/jpeg"), text_block("...")], timeout_s=180)
result.ok, result.output, result.error, result.retryable, result.limits
```

- `run()` does not raise for expected failures. `error` is `refused`, `invalid_output`, `limit`, `auth`, `timeout`, `cli`
  or `runner` (unreachable, busy, or the request was rejected).
- It blocks (up to `timeout_s` plus the runner's queue): call it from a background job, not from a request handler.
- `client` must be the name the token was issued for. `output` is model output: validate it again before using it.
- Tests: `FakeRunner(RunResult(...), ...)` answers in order (the last one repeats) and records `calls`.

## kami_flask.sync (optional, `kami-flask[sync]`)

The offline-first sync server for apps with an Android client (extracted from tasks in 0.3). SQLite only, and
SQLAlchemy comes in through the extra: `kami-flask[sync] @ https://github.com/Kamigiri/kami-flask/archive/refs/tags/v0.4.0.tar.gz`.

The app describes its synced entities and passes either its own Flask-SQLAlchemy `db` (`register`, the library owns
no metadata) or, for plain SQLAlchemy, its declarative base and a session getter (`register_sqlalchemy`, below).
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
  that were never loaded), `collections`, `restamp_where`, `restamp_tombstone`, `id_attr` (below).
- **Handlers** `handler(ctx, op, entity_id, fields) -> bool` run in the request transaction. They need not commit
  or flush: the library flushes, then commits, or rolls back when the op is rejected or fails. Return `True` to
  restamp the row after the op (even a no-op update: the client skipped that row's server version while its op
  was queued). Raise `Rejected(code, message)` to drop the op. An exception with a 4xx `status_code`
  (`spec.rejected_status`: 400, 404, 409) is rejected as well; anything else is `failed`, which stops the
  batch so the client retries (the message is never echoed).
- **Feed**: entities and tombstones merged in `change_seq` order, `limit` (default 500, max 1000), the cursor
  is capped at the `last_seq` read first, `StaleCursor` (410) when the cursor is below `pruned_below` or ahead
  of the server.
- **Maintenance**: `sync.prune()` (tombstones after 90 days, applied ops after 30; run it daily from a
  `job()`), `sync.force_resync()` (after a restore: every client resyncs from 0). Both commit; pass a session to
  use another one than the app's.
- **Parent stamping** (`stamps_parents`): adding, changing or deleting the child restamps the parent. A child that
  leaves a `delete-orphan` collection has no parent any more: list that collection in the parent's `collections`.
- Table names are fixed: `sync_state`, `tombstones`, `applied_ops`. Bulk `query.update()` / `query.delete()` and
  core `insert` / `update` / `delete` skip the flush listener. Write through the ORM, or call
  `sync.stamp(session, obj)` for each row a bulk update touched (same transaction, the caller commits). Deletes
  have no such helper: they go through the ORM.

### Wire ids (`id_attr`)

`Entity(..., id_attr="uid")` makes `uid` the id on the wire (tombstones, `tombstone_children`, the restamp after an
op) while the primary key stays the identity inside the database: foreign keys and `parent=(Model, fk_attr)` keep
pointing at it. `serialize` must emit the same value as `"id"`, and handlers look rows up by it.

- A column default (`default=lambda: uuid4().hex`) is applied at insert; a client-created row must set the uid it
  was sent.
- Composite ids use `id_of`, `id_re` and `restamp_where` as before. `id_of` is also called for pending objects (that
  is when a re-created row loses its tombstone), so it must cope with only the foreign keys being set.
- `tombstone_children` reads the child entity's `id_attr` column, so it does not work for composite child ids
  (use `parent=`, or let the ORM delete the children).

### Plain SQLAlchemy

```python
from kami_flask.sync import Entity, SyncSpec, register_sqlalchemy

from app.listeners import derive_columns  # the app's own before_flush listener: must be registered first

sync = register_sqlalchemy(
    Base,                 # the app's declarative_base()
    lambda: g.db,         # session getter for changes / mutations / prune (or a scoped_session)
    SPEC,
    bind=engine,          # the bookkeeping tables are created here, now
    run_after=(derive_columns,),
)
```

- **Stamping** is attached to the `Session` class, so every session stamps: request sessions, background threads,
  `Session(bind=connection)`, other `sessionmaker`s. A process only stamps after it has called
  `register_sqlalchemy`, so scripts that write synced tables must call it too.
- **Listener order**: `before_flush` listeners run in registration order. An app listener that changes synced
  columns must be registered on `Session` before `register_sqlalchemy`; name it in `run_after` and registration
  fails with `ValueError` if it is not there yet.
- **No clashes**: the bookkeeping models live on the library's own registry and `MetaData`, so an app class or table
  such as `SyncState` / `sync_states` is no problem. They are created on `bind` and by any later
  `Base.metadata.create_all`.
- **Handlers** work on the getter's session and never commit.
- Idempotent per base (`create_app` runs many times in tests); the first spec stays.
- The routes are the same as above. A job without a request passes its session: `sync.prune(session)`.

Install (apps pin a release tag; no git needed in Docker):

```
kami-flask @ https://github.com/Kamigiri/kami-flask/archive/refs/tags/v0.1.0.tar.gz
```

Constraints: one gunicorn worker per app (metrics and the login throttle live in the
process; use threads for concurrency).

Development: `python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"`,
then `python -m pytest` and `ruff check .`. Release steps: `CHANGELOG.md`.
Design notes live in the (private) platform repo.
