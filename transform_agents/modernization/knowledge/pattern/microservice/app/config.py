"""12-factor settings: everything comes from the environment, nothing secret lives in code or the image."""

import os
from collections.abc import Mapping
from dataclasses import dataclass


def _flag(value: str | None, default: bool = False) -> bool:
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def normalize_database_url(url: str) -> str:
    """Use the psycopg 3 driver for PostgreSQL URLs (RDS secrets often say postgres://)."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def database_url_from(env: Mapping[str, str]) -> str:
    """DATABASE_URL wins; otherwise build one from DB_HOST/DB_USER/DB_PASSWORD/DB_NAME (RDS secret keys)."""
    if env.get("DATABASE_URL"):
        return normalize_database_url(env["DATABASE_URL"])
    if env.get("DB_HOST"):
        user, password = env.get("DB_USER", "app"), env.get("DB_PASSWORD", "")
        port, name = env.get("DB_PORT", "5432"), env.get("DB_NAME", "app")
        return f"postgresql+psycopg://{user}:{password}@{env['DB_HOST']}:{port}/{name}"
    return "sqlite:////tmp/app.db"  # local default; /tmp is an emptyDir in Kubernetes


@dataclass(frozen=True)
class Settings:
    database_url: str
    admin_token: str | None = None  # None means admin-only routes are always forbidden
    log_level: str = "INFO"
    seed_demo_data: bool = False
    health_path: str = "/healthz"
    readiness_path: str = "/readyz"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        return cls(
            database_url=database_url_from(env),
            admin_token=env.get("ADMIN_TOKEN") or None,
            log_level=env.get("LOG_LEVEL", "INFO").upper(),
            seed_demo_data=_flag(env.get("SEED_DEMO_DATA")),
            health_path=env.get("HEALTH_PATH", "/healthz"),
            readiness_path=env.get("READINESS_PATH", "/readyz"),
        )
