"""
conftest.py — Pytest configuration for hermetic unit and integration testing.

Sets DATABASE_URL to a local SQLite database BEFORE any app modules are imported.
Spins up an in-process FakePaySwift server for payment testing.
All tests run in SQLite with synthetic data only (no data/*.csv reading, no external network).
"""

import os
import tempfile
import pytest

# CRITICAL: Set DATABASE_URL to SQLite BEFORE any app import
_TEST_DB_PATH = os.path.join(tempfile.gettempdir(), "test_dispute_agent_stage2.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB_PATH}"

from app.db.session import engine, Base, SessionLocal
from tests.fake_payswift import FakePaySwift


@pytest.fixture(scope="session")
def payswift_server():
    """Session-scoped fake PaySwift server."""
    server = FakePaySwift()
    server.start()
    yield server
    server.stop()


@pytest.fixture(autouse=True)
def clean_db(payswift_server):
    """Automatically drops and recreates all tables before each test and resets fake server."""
    payswift_server.reset()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
