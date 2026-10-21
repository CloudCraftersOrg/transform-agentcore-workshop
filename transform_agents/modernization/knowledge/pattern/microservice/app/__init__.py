"""Application factory: settings from env, JSON logs, DB engine, health probes, domain API, JSON errors."""

import logging
import time

from flask import Flask, g, jsonify, request
from werkzeug.exceptions import HTTPException

from . import db, health
from .config import Settings
from .logjson import configure_logging

log = logging.getLogger("app")


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    app = Flask(__name__)
    app.config["SETTINGS"] = settings
    db.init_app(app, settings.database_url)
    health.register(app, settings.health_path, settings.readiness_path)

    from .api import bp as api_bp  # domain routes ported from the legacy monolith

    app.register_blueprint(api_bp)
    _register_observability(app, {settings.health_path, settings.readiness_path})
    return app


def _register_observability(app: Flask, quiet_paths: set[str]) -> None:
    @app.before_request
    def _start_timer():
        g.started = time.perf_counter()

    @app.after_request
    def _log_request(response):
        ms = round((time.perf_counter() - g.get("started", time.perf_counter())) * 1000, 1)
        level = logging.DEBUG if request.path in quiet_paths else logging.INFO
        log.log(
            level,
            "request",
            extra={"method": request.method, "path": request.path, "status": response.status_code, "duration_ms": ms},
        )
        return response

    @app.errorhandler(HTTPException)
    def _http_error(exc: HTTPException):
        return jsonify({"error": exc.description}), exc.code

    @app.errorhandler(Exception)
    def _unhandled(exc: Exception):
        log.exception("unhandled error")
        return jsonify({"error": "internal server error"}), 500
