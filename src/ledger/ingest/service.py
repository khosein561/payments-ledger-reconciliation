"""
The processing pipeline for ONE event: redact -> record once -> handle -> mark.

WHY THIS FILE EXISTS: the web endpoint and the local simulator both need exactly the
same "process an event safely" behaviour. Putting it here, away from FastAPI, means
there is one implementation to test and no way for the two paths to drift apart.

Failure policy (decided deliberately; see docs/runbook.md):
  * Database temporarily unreachable (OperationalError) -> re-raise. The web layer
    answers 503, so Stripe retries later. Nothing was lost.
  * Anything else (bad data, unsupported currency, accounting rule violated) -> the
    event can never succeed by retrying, so we PARK it in dead_letter_events and
    acknowledge it. Otherwise Stripe would retry a poison event for days.
"""

from __future__ import annotations

import logging

from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError

from ..config import get_pii_key
from ..security.pii import redact_payload
from .event_handlers import handle_event, send_to_dead_letter
from .idempotency import mark_event, record_event_once

log = logging.getLogger("ledger.ingest")


def process_event(engine: Engine, event: dict, pii_key: bytes | None = None) -> str:
    """Process one verified event. Returns 'processed', 'ignored', 'duplicate' or 'dead_letter'."""
    key = pii_key if pii_key is not None else get_pii_key()
    redacted = redact_payload(event, key)

    try:
        with engine.begin() as conn:
            if not record_event_once(conn, event["id"], event["type"], redacted):
                return "duplicate"
            status = handle_event(conn, event)
            mark_event(conn, event["id"], status)
            return status
    except OperationalError:
        raise  # transient infrastructure problem: let the caller ask the sender to retry
    except Exception as exc:  # noqa: BLE001 - deliberate: any other failure is parked, not lost
        log.exception("event %s could not be processed; moving to dead letter", event.get("id"))
        with engine.begin() as conn:
            send_to_dead_letter(conn, redacted, exc)
        return "dead_letter"


def replay_dead_letter(engine: Engine, dead_letter_id: int) -> str:
    """Retry a parked event after the cause has been fixed. Returns the new status.

    The stored payload is already redacted, which is fine: the accounting handlers
    only use amounts, ids and timestamps.
    """
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT event_id, payload FROM dead_letter_events WHERE id = :id AND resolved_at IS NULL"),
            {"id": dead_letter_id},
        ).fetchone()
    if row is None:
        raise KeyError(f"no open dead-letter event with id {dead_letter_id}")

    event = row.payload
    try:
        with engine.begin() as conn:
            status = handle_event(conn, event)
            conn.execute(
                text("UPDATE webhook_events SET status = :s, processed_at = now() WHERE event_id = :eid"),
                {"s": status, "eid": row.event_id},
            )
            conn.execute(
                text(
                    "UPDATE dead_letter_events SET resolved_at = now(), retry_count = retry_count + 1 WHERE id = :id"
                ),
                {"id": dead_letter_id},
            )
        return status
    except OperationalError:
        raise
    except Exception as exc:  # noqa: BLE001
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE dead_letter_events SET retry_count = retry_count + 1, error = :err WHERE id = :id"
                ),
                {"id": dead_letter_id, "err": f"{type(exc).__name__}: {exc}"},
            )
        return "dead_letter"
