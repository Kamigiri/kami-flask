"""JSON errors: ``{"error": message}`` for every HTTP error and unhandled exception.

Apps can still register handlers for their own exception classes; Flask prefers the most
specific handler, so those win over the generic ones here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from flask import Flask, jsonify
from werkzeug.exceptions import HTTPException

from .logs import request_context
from .metrics import APP_ERRORS

if TYPE_CHECKING:
    from .app import KamiApp

log = logging.getLogger("kami.errors")


def init(app: Flask, kami: KamiApp) -> None:
    @app.errorhandler(HTTPException)
    def _http_error(error: HTTPException):
        # Werkzeug's default descriptions are long sentences; a custom abort(code, "msg") wins.
        default = error.description == type(error).description
        response = jsonify({"error": error.name if default else error.description})
        response.status_code = error.code or 500
        for key, value in error.get_headers():
            if key.lower() != "content-type":
                response.headers[key] = value  # e.g. Allow on 405
        return response

    @app.errorhandler(Exception)
    def _unhandled(error: Exception):
        if isinstance(error, HTTPException):
            return _http_error(error)
        log.exception("Unhandled error", extra=request_context())
        APP_ERRORS.inc()
        for hook in kami.error_hooks:
            try:
                hook(error)
            except Exception:
                log.exception("on_error hook failed")
        return jsonify({"error": "Internal server error"}), 500
