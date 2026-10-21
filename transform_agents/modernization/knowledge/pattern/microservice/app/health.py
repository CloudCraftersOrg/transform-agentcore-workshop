"""Liveness (process is up) and readiness (database reachable) endpoints for Kubernetes probes."""

import logging

from flask import Blueprint, Flask
from sqlalchemy.exc import SQLAlchemyError

from .db import fetch_one

log = logging.getLogger(__name__)


def register(app: Flask, health_path: str, readiness_path: str) -> None:
    bp = Blueprint("health", __name__)

    @bp.get(health_path)
    def healthz():
        return {"status": "ok"}

    @bp.get(readiness_path)
    def readyz():
        try:
            fetch_one("SELECT 1 AS ok")
        except SQLAlchemyError as exc:
            log.warning("readiness check failed", extra={"error": type(exc).__name__})
            return {"status": "unavailable"}, 503
        return {"status": "ready"}

    app.register_blueprint(bp)
