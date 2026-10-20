"""WSGI entrypoint for gunicorn: `gunicorn -c gunicorn.conf.py app.wsgi:app`."""

from . import create_app

app = create_app()
