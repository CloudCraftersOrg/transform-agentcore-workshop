"""Nightly report job: `python -m app.report` (a Kubernetes CronJob replaces the legacy cron + local CSV file).

The CSV goes to stdout (collected by the cluster log pipeline) or, when REPORT_S3_URI=s3://bucket/prefix is
set, to Amazon S3 (needs boto3 in requirements.txt and IRSA/Pod Identity). Logs go to stderr.
"""

import csv
import io
import logging
import os
import sys
from datetime import date

from sqlalchemy import Engine, text

from .config import Settings
from .db import make_engine, migrate, seed
from .logjson import configure_logging

# --- domain mapping (ported from the legacy report) ---------------------------------------------
REPORT_NAME = "report"
COLUMNS = ["id"]
QUERY = "SELECT id FROM example ORDER BY id"
# ------------------------------------------------------------------------------------------------

log = logging.getLogger("app.report")


def build_report(engine: Engine) -> tuple[str, int]:
    """Render the report as CSV text; returns (csv, row_count)."""
    with engine.connect() as conn:
        rows = conn.execute(text(QUERY)).mappings().all()
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    writer.writerows([row[c] for c in COLUMNS] for row in rows)
    return buffer.getvalue(), len(rows)


def upload_to_s3(uri: str, body: str, filename: str) -> str:
    import boto3  # optional dependency, only needed when REPORT_S3_URI is set

    bucket, _, prefix = uri.removeprefix("s3://").partition("/")
    key = f"{prefix.rstrip('/')}/{filename}".lstrip("/")
    boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=body.encode(), ContentType="text/csv")
    return f"s3://{bucket}/{key}"


def main() -> int:
    settings = Settings.from_env()
    configure_logging(settings.log_level, stream=sys.stderr)
    engine = make_engine(settings.database_url)
    try:
        migrate(engine)
        if settings.seed_demo_data:
            seed(engine)
        body, count = build_report(engine)
    finally:
        engine.dispose()
    filename = f"{REPORT_NAME}-{date.today().isoformat()}.csv"
    if uri := os.getenv("REPORT_S3_URI"):
        log.info("report uploaded", extra={"rows": count, "target": upload_to_s3(uri, body, filename)})
    else:
        sys.stdout.write(body)
        log.info("report written to stdout", extra={"rows": count, "report": filename})
    return 0


if __name__ == "__main__":
    sys.exit(main())
