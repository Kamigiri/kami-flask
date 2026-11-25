# Changelog — kami-flask

Apps pin a tag: `kami-flask @ https://github.com/Kamigiri/kami-flask/archive/refs/tags/vX.Y.Z.tar.gz`.
Release: move **Unreleased** to a dated version, bump `pyproject.toml` and
`src/kami_flask/__init__.py`, commit, tag `vX.Y.Z`, push the tag.

## Unreleased

## 0.4.0 (2026-11-25)

- `kami_flask.claude_runner`: client for the platform's internal Claude runner (headless Claude Code without tools):
  `RunnerClient(url, token, client)` with `run(...)` -> `RunResult` and `limits()`, `image_block` / `text_block`, and
  `FakeRunner` (same interface) for app tests. Standard library only, no new dependency.
- `kami_flask.sync` works with plain SQLAlchemy (additive; `register(db, spec)` is unchanged):
  `register_sqlalchemy(base, session, spec, bind=..., run_after=...)` stamps on the `Session` class (every session),
  checks that the app's own `before_flush` listeners run first, and keeps its bookkeeping models on a separate
  registry (same table names).
- `Entity.id_attr` (default `id`): a wire-only id such as `uid` on an integer-keyed model, used by the feed's
  tombstones, `tombstone_children` and the restamp. Foreign keys keep using the primary key.
- `Sync.stamp(session, obj)` for writes that skip the flush listener. `prune()` / `force_resync()` may be called
  without a session.
- Handlers need not commit or flush: the library flushes after the handler, so a constraint error counts against
  that op (`failed`) instead of ending the request with a 500.
- Behaviour fix, both ways of registering: a `stamps_parents` child now also restamps its parent when it is deleted,
  and when it is added with only its foreign key set.

## 0.3.0 (2026-11-12)

- `kami_flask.sync` (optional extra `kami-flask[sync]`, SQLAlchemy only there): the offline-first sync server
  extracted from tasks. `register(db, SyncSpec(...))` stamps `change_seq` on every flush, writes tombstones
  (parent-covers-child, `tombstone_children`, `StampsParent`), serves the change feed with the cursor cap and 410
  (`StaleCursor`), applies the mutation queue (statuses applied | duplicate | rejected | failed, 4xx -> rejected,
  restamping of untouched rows, one handler per entity) and offers `prune` and `force_resync`.
- The bookkeeping tables keep the tasks names (`sync_state`, `tombstones`, `applied_ops`, `change_seq`), so
  existing databases need no migration. The library owns no metadata: the models are built on the app's `db`.
- Apps without `[sync]` gain no dependency.

## 0.2.0 (2026-10-16)

- Gateway auth next to the local login: with `GATEWAY_SECRET` set, `Remote-User` is trusted only
  when `X-Kami-Gateway` carries the secret (constant-time compare). Order: gateway, Bearer, session.
  Without a valid secret the header is ignored. Gateway writes still need `X-Requested-With`.
- `/api/auth/me` reports `{"auth": "gateway", "logout_url": GATEWAY_LOGOUT_URL}` for gateway users;
  request logs show the gateway user; a short `GATEWAY_SECRET` is flagged by `unsafe_settings()`.
- pytest plugin: `gateway_client` fixture.

## 0.1.0 (2026-10-16)

- `KamiApp`: settings with production refusal of default secrets, single-user auth
  (session + Bearer token, throttle, XHR header), `/api/health`, `/api/ready`,
  `/api/version`, JSON request logs with request id, Prometheus metrics on
  `METRICS_PORT`, `job()` tracking, `{"error": msg}` errors, SPA serving.
- pytest plugin: `KamiTestClient`, `client`, `anon_client`, `bearer_client`.
- `job(...).mark(result)` for runs that report failure without raising.
- The SPA is only served from a directory that contains `index.html` (an empty `WEB_DIST`
  never exposes the working directory); `/api/ready` never returns error details; a busy
  `METRICS_PORT` logs a warning instead of crashing CLI scripts; short `API_TOKEN`s are refused
  in production; request ids are capped at 64 characters; the login throttle is thread-safe.
