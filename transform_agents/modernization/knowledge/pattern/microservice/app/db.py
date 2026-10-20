"""Database access with SQLAlchemy Core: portable between SQLite (local/kind) and PostgreSQL (Amazon RDS).

Domain code uses these helpers with *bound parameters only* (":name" placeholders), never string formatting.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from flask import Flask, current_app
from sqlalchemy import Connection, Engine, TextClause, create_engine, event, func, select, text

from .models import metadata


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"timeout": 15, "check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # enforce FKs like PostgreSQL does
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5, pool_recycle=1800)


def init_app(app: Flask, url: str) -> None:
    app.extensions["engine"] = make_engine(url)


def engine() -> Engine:
    return current_app.extensions["engine"]


Params = Mapping[str, Any] | None


def _bind(sql: str | TextClause, params: Params, kwargs: dict[str, Any]) -> tuple[TextClause, dict[str, Any]]:
    """Accept both call styles: fetch_all(sql, id=1) and fetch_all(text(sql), {"id": 1})."""
    return (text(sql) if isinstance(sql, str) else sql), {**(params or {}), **kwargs}


def fetch_all(sql: str | TextClause, params: Params = None, /, **kwargs: Any) -> list[dict[str, Any]]:
    """Run a SELECT and return every row as a dict."""
    with engine().connect() as conn:
        return [dict(row) for row in conn.execute(*_bind(sql, params, kwargs)).mappings()]


def fetch_one(sql: str | TextClause, params: Params = None, /, **kwargs: Any) -> dict[str, Any] | None:
    """Run a SELECT and return the first row as a dict (or None)."""
    with engine().connect() as conn:
        row = conn.execute(*_bind(sql, params, kwargs)).mappings().first()
        return dict(row) if row else None


def execute(sql: str | TextClause, params: Params = None, /, **kwargs: Any) -> int:
    """Run one INSERT/UPDATE/DELETE in its own transaction and return the affected row count."""
    with engine().begin() as conn:
        return conn.execute(*_bind(sql, params, kwargs)).rowcount


@contextmanager
def transaction() -> Iterator[Connection]:
    """BEGIN ... COMMIT (ROLLBACK on exception). Use conn.execute(text(sql), {...}) inside."""
    with engine().begin() as conn:
        yield conn


def migrate(eng: Engine) -> None:
    """Create missing tables. Safe to run from many pods at once (advisory lock on PostgreSQL)."""
    with eng.begin() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(424242)"))
        metadata.create_all(conn)


def seed(eng: Engine) -> bool:
    """Load app/seed.sql once, when the first table is still empty. Returns True if rows were inserted."""
    seed_file = Path(__file__).with_name("seed.sql")
    first_table = next(iter(metadata.sorted_tables), None)
    if first_table is None or not seed_file.exists():
        return False
    with eng.begin() as conn:
        if conn.execute(select(func.count()).select_from(first_table)).scalar_one():
            return False
        sql = "\n".join(line for line in seed_file.read_text().splitlines() if not line.lstrip().startswith("--"))
        for statement in filter(str.strip, sql.split(";")):
            conn.exec_driver_sql(statement)
    return True
