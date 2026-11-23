"""
Exactly-once recording of webhook events.

WHY THIS FILE EXISTS: Stripe (like every webhook provider) delivers AT LEAST ONCE. If
our server is slow, or crashes after processing but before replying, Stripe sends the
same event again. Without protection we would post the same payment twice.

The fix is an idempotency key: the event id is a PRIMARY KEY, so the database itself
decides who is first. INSERT ... ON CONFLICT DO NOTHING either inserts (first time) or
does nothing (duplicate), and tells us which with RETURNING.
"""

from __future__ import annotations

import json

from sqlalchemy import Connection, text


def record_event_once(conn: Connection, event_id: str, event_type: str, payload: dict) -> bool:
    """Return True if this is the first time we see `event_id`, False for a duplicate."""
    row = conn.execute(
        text(
            """
            INSERT INTO webhook_events (event_id, event_type, payload)
            VALUES (:eid, :etype, CAST(:payload AS JSONB))
            ON CONFLICT (event_id) DO NOTHING
            RETURNING event_id
            """
        ),
        {"eid": event_id, "etype": event_type, "payload": json.dumps(payload)},
    ).fetchone()
    return row is not None


def mark_event(conn: Connection, event_id: str, status: str) -> None:
    """Record the outcome ('processed' or 'ignored') on the webhook_events row."""
    conn.execute(
        text("UPDATE webhook_events SET status = :s, processed_at = now() WHERE event_id = :eid"),
        {"s": status, "eid": event_id},
    )
