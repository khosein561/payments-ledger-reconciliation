"""
Shared test fixtures.

The tests run against a REAL PostgreSQL, because the most important guarantees in
this project (balanced entries, append-only history, idempotent inserts, roles) are
enforced by the database itself. Mocking the database would test nothing that matters.

Setup: start Postgres (`docker compose up -d postgres`) and either
  * set LEDGER_TEST_ADMIN_URL to a superuser URL, e.g.
        postgresql+psycopg2://ledger:change_me@localhost:5432/postgres
  * or have POSTGRES_USER / POSTGRES_PASSWORD (and optionally HOST / PORT) in .env.
Each test session creates a throw-away database and drops it afterwards.
"""

from __future__ import annotations

import os
import uuid
from urllib.parse import quote_plus

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from ledger.core.accounts import seed_accounts
from ledger.migrate import apply_migrations

PII_KEY = b"test-pii-key"
WEBHOOK_SECRET = "whsec_test_secret"

ALL_TABLES = (
    "recon_breaks, recon_matches, recon_runs, bank_settlement, processor_settlement, dead_letter_events, "
    "webhook_events, audit_log, postings, journal_entries, accounts"
)


def _admin_url() -> str | None:
    load_dotenv()
    explicit = os.getenv("LEDGER_TEST_ADMIN_URL")
    if explicit:
        return explicit
    user, password = os.getenv("POSTGRES_USER"), os.getenv("POSTGRES_PASSWORD")
    if not (user and password):
        return None
    host, port = os.getenv("POSTGRES_HOST", "localhost"), os.getenv("POSTGRES_PORT", "5432")
    return f"postgresql+psycopg2://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/postgres"


@pytest.fixture(scope="session")
def engine():
    admin_url = _admin_url()
    if not admin_url:
        pytest.skip("No test database configured. Set LEDGER_TEST_ADMIN_URL (see tests/conftest.py).")

    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    name = f"ledger_test_{uuid.uuid4().hex[:8]}"
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Cannot reach Postgres to create a test database ({type(exc).__name__}). Is it running?")

    eng = create_engine(make_url(admin_url).set(database=name))
    apply_migrations(eng)
    yield eng
    eng.dispose()
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture()
def db(engine):
    """A clean, seeded database for each test."""
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {ALL_TABLES} RESTART IDENTITY CASCADE"))
        seed_accounts(conn)
    return engine


def count(engine, table: str, where: str = "TRUE") -> int:
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT count(*) FROM {table} WHERE {where}")).scalar_one()
