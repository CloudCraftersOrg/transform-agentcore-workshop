"""Shared fixtures: an app on a throwaway database (SQLite by default, PostgreSQL via TEST_DATABASE_URL)."""

import os

import pytest
from app import create_app
from app.config import Settings, normalize_database_url
from app.db import migrate, seed
from app.models import metadata

ADMIN_TOKEN = "test-admin-token"


@pytest.fixture
def app(tmp_path):
    url = normalize_database_url(os.getenv("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'test.db'}")
    application = create_app(Settings(database_url=url, admin_token=ADMIN_TOKEN, log_level="WARNING"))
    engine = application.extensions["engine"]
    metadata.drop_all(engine)
    migrate(engine)
    yield application
    metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def engine(app):
    return app.extensions["engine"]


@pytest.fixture
def seeded(engine):
    """Load app/seed.sql (the legacy seed data) into the test database."""
    seed(engine)
    return engine


@pytest.fixture
def admin_token():
    return ADMIN_TOKEN
