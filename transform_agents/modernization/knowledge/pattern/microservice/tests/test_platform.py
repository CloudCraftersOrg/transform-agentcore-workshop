"""Platform tests from the pattern: health probes, JSON errors and logs, 12-factor config, report job, SQL hygiene."""

import io
import json
import logging
import re
from pathlib import Path

from app import create_app
from app.config import Settings
from app.logjson import JsonFormatter
from app.report import COLUMNS, build_report

APP_DIR = Path(__file__).resolve().parents[1] / "app"


def test_liveness(client, app):
    response = client.get(app.config["SETTINGS"].health_path)
    assert response.status_code == 200 and response.get_json() == {"status": "ok"}


def test_readiness_checks_database(client, app):
    response = client.get(app.config["SETTINGS"].readiness_path)
    assert response.status_code == 200 and response.get_json() == {"status": "ready"}


def test_readiness_fails_without_database(tmp_path):
    broken = create_app(Settings(database_url=f"sqlite:///{tmp_path}/missing/dir/x.db", log_level="CRITICAL"))
    response = broken.test_client().get(broken.config["SETTINGS"].readiness_path)
    assert response.status_code == 503


def test_unknown_route_returns_json_error(client):
    response = client.get("/definitely-not-a-route")
    assert response.status_code == 404 and "error" in response.get_json()


def test_settings_come_from_environment():
    settings = Settings.from_env(
        {"DATABASE_URL": "postgres://u:p@db:5432/app", "ADMIN_TOKEN": "t", "LOG_LEVEL": "debug"}
    )
    assert settings.database_url == "postgresql+psycopg://u:p@db:5432/app"
    assert settings.admin_token == "t" and settings.log_level == "DEBUG"
    assert Settings.from_env({}).admin_token is None


def test_logs_are_single_line_json():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("test.json")
    logger.addHandler(handler)
    logger.warning("hello", extra={"order_id": 7})
    record = json.loads(stream.getvalue())
    assert record["msg"] == "hello" and record["order_id"] == 7 and record["level"] == "WARNING"


def test_report_job_renders_csv(seeded):
    body, rows = build_report(seeded)
    lines = body.splitlines()
    assert lines[0] == ",".join(COLUMNS)
    assert len(lines) == rows + 1


def test_no_hardcoded_secrets():
    pattern = re.compile(r"(?i)\b(\w*(?:secret|token|password|passwd)\w*)\s*=\s*['\"][^'\"]{3,}['\"]")
    offenders = [
        f"{p.name}:{m.group(0)}"
        for p in APP_DIR.glob("*.py")
        for m in pattern.finditer(p.read_text())
        if "header" not in m.group(1).lower()
    ]
    assert not offenders, f"hard-coded credentials: {offenders}"


def test_sql_uses_bound_parameters():
    unsafe = re.compile(
        r"(text|execute|exec_driver_sql|fetch_all|fetch_one)\(\s*(f['\"]|['\"][^'\"]*['\"]\s*(\+|%|\.format))"
    )
    offenders = [f"{p.name}:{m.group(0)}" for p in APP_DIR.glob("*.py") for m in unsafe.finditer(p.read_text())]
    assert not offenders, f"SQL built with string formatting: {offenders}"
