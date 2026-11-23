"""
Tiny migration runner.

WHY THIS FILE EXISTS: the schema is code. Numbered .sql files applied in order, each
exactly once, mean a fresh database and a production database end up identical, and
the history of every schema change is in git. (dbt/Flyway/Alembic do this at scale;
this is the 40-line version that shows you understand the idea.)
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import Engine

# The SQL files live next to the package in a source checkout. When the package is installed
# somewhere else (e.g. inside a container), LEDGER_MIGRATIONS_DIR points at them instead.
DEFAULT_DIR = Path(os.environ.get("LEDGER_MIGRATIONS_DIR") or Path(__file__).resolve().parents[2] / "db" / "migrations")


def apply_migrations(engine: Engine, directory: Path | None = None) -> list[str]:
    """Apply every not-yet-applied migration in filename order. Returns the versions applied."""
    directory = Path(directory) if directory else DEFAULT_DIR
    files = sorted(directory.glob("*.sql"))
    applied: list[str] = []

    # We use the raw DBAPI connection because the SQL files contain '%' (in RAISE
    # messages) and several statements per file; passing no parameters keeps psycopg2
    # from trying to interpret the '%' characters.
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
        raw.commit()

        for path in files:
            version = path.stem
            with raw.cursor() as cur:
                cur.execute("SELECT 1 FROM schema_migrations WHERE version = %s", (version,))
                if cur.fetchone():
                    continue
                try:
                    cur.execute(path.read_text(encoding="utf-8"))
                    cur.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
                    raw.commit()  # each migration is atomic: all of it or none of it
                except Exception:
                    raw.rollback()
                    raise
            applied.append(version)
    finally:
        raw.close()
    return applied
