"""
Turn processor events into ledger entries.

WHY THIS FILE EXISTS: this is the business logic that connects "Stripe said X
happened" to "here are the debits and credits". Keeping it separate from the web
layer means you can test the accounting without HTTP, and swap the processor without
touching the ledger.

Simplifications (also listed in the README's Limitations):
  * USD only. Any other currency raises, which sends the event to the dead-letter
    table instead of silently mis-posting.
  * The processor fee is computed from a fixed schedule (2.9% + 30 cents), not read
    from Stripe's balance transaction. Real Stripe fees vary by card and country.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from sqlalchemy import Connection, text

from ..core.posting import Line, create_entry


def processor_fee(amount_minor: int) -> int:
    """Fee in cents: 2.9% + 30 cents, rounded half-up using integer math only."""
    return (amount_minor * 29 + 500) // 1000 + 30


def _date_from_unix(ts: int) -> date:
    return datetime.fromtimestamp(ts, tz=timezone.utc).date()


def _currency(obj: dict) -> None:
    cur = str(obj.get("currency", "")).upper()
    if cur != "USD":
        raise ValueError(f"unsupported currency {cur!r}; only USD is handled")


def on_charge_succeeded(conn: Connection, event: dict) -> str:
    obj = event["data"]["object"]
    _currency(obj)
    gross = obj["amount"]
    fee = processor_fee(gross) if isinstance(gross, int) and not isinstance(gross, bool) else 0
    lines = [
        Line("PROCESSOR_CLEARING", "debit", gross),
        Line("REVENUE", "credit", gross),
        Line("FEES_EXPENSE", "debit", fee),
        Line("PROCESSOR_CLEARING", "credit", fee),
    ]
    create_entry(
        conn, event["id"], "charge", f"Charge {obj['id']}", lines,
        business_date=_date_from_unix(event["created"]), external_ref=obj["id"],
    )
    return "processed"


def on_refund(conn: Connection, event: dict) -> str:
    obj = event["data"]["object"]
    _currency(obj)
    amount = obj["amount"]
    lines = [Line("REFUNDS", "debit", amount), Line("PROCESSOR_CLEARING", "credit", amount)]
    create_entry(
        conn, event["id"], "refund", f"Refund {obj['id']}", lines,
        business_date=_date_from_unix(event["created"]), external_ref=obj["id"],
    )
    return "processed"


def on_payout_paid(conn: Connection, event: dict) -> str:
    obj = event["data"]["object"]
    _currency(obj)
    amount = obj["amount"]
    lines = [Line("CASH", "debit", amount), Line("PROCESSOR_CLEARING", "credit", amount)]
    create_entry(
        conn, event["id"], "payout", f"Payout {obj['id']}", lines,
        business_date=_date_from_unix(obj.get("arrival_date", event["created"])), external_ref=obj["id"],
    )
    return "processed"


HANDLERS = {
    "charge.succeeded": on_charge_succeeded,
    "refund.created": on_refund,
    "payout.paid": on_payout_paid,
}


def handle_event(conn: Connection, event: dict) -> str:
    """Dispatch on event type. Unknown types are acknowledged but ignored (not an error)."""
    handler = HANDLERS.get(event["type"])
    if handler is None:
        return "ignored"
    return handler(conn, event)


def send_to_dead_letter(conn: Connection, event: dict, error: Exception) -> None:
    """Park an event we could not process so a human can fix the cause and replay it.

    `event` must already be PII-redacted. We also mark the webhook_events row as
    dead_letter so a redelivery of the same event is treated as a duplicate.
    """
    payload = json.dumps(event)
    conn.execute(
        text(
            """
            INSERT INTO dead_letter_events (event_id, payload, error)
            VALUES (:eid, CAST(:payload AS JSONB), :err)
            """
        ),
        {"eid": event.get("id", "unknown"), "payload": payload, "err": f"{type(error).__name__}: {error}"},
    )
    conn.execute(
        text(
            """
            INSERT INTO webhook_events (event_id, event_type, payload, status)
            VALUES (:eid, :etype, CAST(:payload AS JSONB), 'dead_letter')
            ON CONFLICT (event_id) DO UPDATE SET status = 'dead_letter'
            """
        ),
        {"eid": event.get("id", "unknown"), "etype": event.get("type", "unknown"), "payload": payload},
    )
