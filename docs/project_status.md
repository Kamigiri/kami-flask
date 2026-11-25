# Project Status: kami-flask

> Last updated: 2026-11-25

## Now

- **v0.4.0** prepared (2026-11-25), not tagged yet. On `main`: `kami_flask.claude_runner` (`RunnerClient`, `FakeRunner`)
  for the platform's Claude runner. On branch `feat/0.4-plain-sqlalchemy`: `kami_flask.sync` on plain SQLAlchemy
  (`register_sqlalchemy`, `id_attr`, `stamp`) for the Finance sync API.
- Before it: v0.3.0 (`kami_flask.sync`). History: [`../CHANGELOG.md`](../CHANGELOG.md).

## Open

- Release 0.4.0: merge `feat/0.4-plain-sqlalchemy`, tag `v0.4.0`, push; then Finance pins it (and apps that want it).

## Next

- Ideas, not scheduled: a migration runner, scoped service tokens (needed by the contracts tracker).
