# kami-flask

The shared Flask backend framework for the Kamigiri apps. One `KamiApp` gives an app:

- **Settings** from the environment, with `APP_ENV=production` refusing default secrets
- **Auth**: single-user session login (`/api/auth/login|logout|me`), optional Bearer
  `API_TOKEN`, login throttle, `X-Requested-With` required for cookie-authed writes
- **Platform contract**: `/api/health` (liveness), `/api/ready` (DB check), `/api/version`
  (version + git revision from the image)
- **Logs**: one JSON line per request on stdout (`request_id`, route, status, duration, user)
- **Metrics**: Prometheus on `METRICS_PORT` (not proxied by nginx), RED metrics per route,
  `app_info`, errors, and background jobs via `job("name")`
- **Errors** as `{"error": msg}` (short names for werkzeug defaults, e.g. `"Not Found"`), **SPA serving** (WhiteNoise + index.html fallback)
- **pytest plugin**: `client` (logged in), `anon_client`, `bearer_client`

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

Install (apps pin a release tag; no git needed in Docker):

```
kami-flask @ https://github.com/Kamigiri/kami-flask/archive/refs/tags/v0.1.0.tar.gz
```

Constraints: one gunicorn worker per app (metrics and the login throttle live in the
process; use threads for concurrency).

Development: `python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"`,
then `python -m pytest` and `ruff check .`. Release steps: `CHANGELOG.md`.
Design notes live in the (private) platform repo.
