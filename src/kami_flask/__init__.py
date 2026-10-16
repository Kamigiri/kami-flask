"""kami-flask: the shared backend framework for Kamigiri apps.

Auth, settings checks, health/ready/version, JSON logs, Prometheus metrics, job tracking,
JSON errors, SPA serving and pytest fixtures, so every app gets the platform contract by
building on :class:`KamiApp`.
"""

from .app import KamiApp
from .jobs import job
from .settings import Settings, env

__version__ = "0.2.0"
__all__ = ["KamiApp", "Settings", "env", "job", "__version__"]
