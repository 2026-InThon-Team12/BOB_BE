import os
from dataclasses import dataclass
from datetime import date
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.core.clock import get_today
from app.core.config import Settings
from app.db import Base
from app.main import create_app


@dataclass
class TestDate:
    value: date = date(2026, 10, 31)


@pytest.fixture
def today():
    return TestDate()


@pytest.fixture
def session_factory(tmp_path):
    url = os.environ.get("TEST_DATABASE_URL")
    admin = None
    schema = "bob_test_" + uuid4().hex
    if url:
        # Each test has a fresh isolated schema; existing tables are never dropped.
        admin = create_engine(url)
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"}, pool_pre_ping=True)
    else:
        engine = create_engine(
            f"sqlite:///{tmp_path / 'test.sqlite'}", connect_args={"check_same_thread": False}
        )

        @event.listens_for(engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    yield factory
    engine.dispose()
    if admin:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def db_session(session_factory):
    with session_factory() as session:
        yield session


@pytest.fixture
def session(db_session):
    return db_session


@pytest.fixture
def app(session_factory, today):
    settings = Settings(_env_file=None, lablup_api_key=None, environment="test")
    application = create_app(settings=settings, session_factory=session_factory)
    application.dependency_overrides[get_today] = lambda: today.value
    return application


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


@pytest.fixture
def guest_headers(client):
    response = client.post("/api/v1/guest-sessions")
    assert response.status_code == 201
    return {"Authorization": "Bearer " + response.json()["access_token"]}
