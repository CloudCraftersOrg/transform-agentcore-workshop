"""gunicorn settings (12-factor: tuned through env vars). Replaces the legacy Flask dev server + systemd."""

import os

bind = f"0.0.0.0:{os.getenv('PORT', '8080')}"
workers = int(os.getenv("WEB_CONCURRENCY", "2"))
threads = int(os.getenv("GUNICORN_THREADS", "4"))
worker_class = "gthread"
timeout = 30
graceful_timeout = 20
keepalive = 5
worker_tmp_dir = os.getenv("GUNICORN_TMP_DIR", "/tmp")  # writable emptyDir with a read-only root FS
control_socket_disable = True  # gunicorn 26 control socket would need a writable $HOME
accesslog = None  # the app logs every request as JSON
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", "info").lower()
logconfig_dict = {  # merged into gunicorn's defaults: keep the "console" handler name
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "app.logjson.JsonFormatter"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json", "stream": "ext://sys.stdout"}},
    "root": {"level": loglevel.upper(), "handlers": ["console"]},
    "loggers": {
        "gunicorn.error": {"level": loglevel.upper(), "handlers": ["console"], "propagate": False},
        "gunicorn.access": {"level": "WARNING", "handlers": ["console"], "propagate": False},  # the app logs requests
    },
}


def on_starting(server):
    """Create/upgrade the schema once per pod, before workers fork (disable with DB_AUTO_MIGRATE=false)."""
    if os.getenv("DB_AUTO_MIGRATE", "true").lower() == "true":
        from app.migrate import main

        main()
