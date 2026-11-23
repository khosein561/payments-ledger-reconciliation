"""
Database helpers.

WHY THIS FILE EXISTS: all writes go through `transaction()` so a group of statements
either ALL happen or NONE do. That atomicity is what makes a ledger trustworthy: you
never see half of a journal entry.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from functools import lru_cache
from typing import Any, Iterator

from sqlalchemy import Connection, Engine, create_engine, text

from .config import get_db_url


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Create (once) and return the shared engine. pool_pre_ping drops dead connections."""
    return create_engine(get_db_url(), pool_pre_ping=True)


@contextmanager
def transaction(engine: Engine | None = None) -> Iterator[Connection]:
    """BEGIN ... COMMIT, with automatic ROLLBACK if the block raises."""
    engine = engine or get_engine()
    with engine.begin() as conn:
        yield conn


def audit(
    conn: Connection,
    actor: str,
    action: str,
    object_type: str,
    object_id: Any = None,
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    """Append a row to the audit log (who did what to which object, and when)."""
    conn.execute(
        text(
            """
            INSERT INTO audit_log (actor, action, object_type, object_id, before, after)
            VALUES (:actor, :action, :object_type, :object_id,
                    CAST(:before AS JSONB), CAST(:after AS JSONB))
            """
        ),
        {
            "actor": actor,
            "action": action,
            "object_type": object_type,
            "object_id": None if object_id is None else str(object_id),
            "before": None if before is None else json.dumps(before, default=str),
            "after": None if after is None else json.dumps(after, default=str),
        },
    )
