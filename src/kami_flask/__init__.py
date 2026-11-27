"""kami-flask: the shared backend framework for Kamigiri apps.

Auth, settings checks, health/ready/version, JSON logs, Prometheus metrics, job tracking, the "needs attention" contract,
JSON errors, SPA serving and pytest fixtures, so every app gets the platform contract by
building on :class:`KamiApp`.
"""

from .app import KamiApp
from .jobs import job
from .settings import Settings, env

__version__ = "0.5.0"
__all__ = ["KamiApp", "Settings", "env", "job", "__version__"]
