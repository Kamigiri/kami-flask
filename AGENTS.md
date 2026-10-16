# Agent Notes: kami-flask

Shared Flask backend framework for the Kamigiri apps (budget, tasks, future apps). Own
public git repo (`Kamigiri/kami-flask`, branch `main`); the platform repo ignores this folder.
Overview and usage: `README.md`. Design notes (private, platform repo): `../../lan/docs/plans/kami-flask.md`; do not link them from public files.

- `src/kami_flask/`: one module per concern (settings, auth, health, logs, metrics, jobs,
  errors, spa, testing); `app.py` wires them in a fixed order (logs/metrics → auth guard →
  app hooks). Keep that order.
- Behaviour changes are platform changes: both apps depend on them. Add a CHANGELOG line
  under **Unreleased**, release with a tag, then bump the pin in each app.
- Public repo: never put secrets, hostnames or personal data in code, tests or docs.
- Before finishing: `python -m pytest` and `ruff check .` (CI runs both on 3.12 and 3.14).
