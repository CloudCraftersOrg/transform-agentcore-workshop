"""Schema migration job: `python -m app.migrate` (also run by gunicorn on start when DB_AUTO_MIGRATE=true)."""

import logging
import sys

from .config import Settings
from .db import make_engine, migrate, seed
from .logjson import configure_logging

log = logging.getLogger("app.migrate")


def main() -> int:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    engine = make_engine(settings.database_url)
    try:
        migrate(engine)
        seeded = seed(engine) if settings.seed_demo_data else False
        log.info("schema ready", extra={"dialect": engine.dialect.name, "seeded": seeded})
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
