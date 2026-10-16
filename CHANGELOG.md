# Changelog — kami-flask

Apps pin a tag: `kami-flask @ https://github.com/Kamigiri/kami-flask/archive/refs/tags/vX.Y.Z.tar.gz`.
Release: move **Unreleased** to a dated version, bump `pyproject.toml` and
`src/kami_flask/__init__.py`, commit, tag `vX.Y.Z`, push the tag.

## Unreleased

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
