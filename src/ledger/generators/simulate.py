"""
Generate realistic-looking (but entirely fake) Stripe-style events.

WHY THIS FILE EXISTS: you cannot demonstrate a reconciliation system without
transactions to reconcile, and you must never use real customer data in a portfolio
project. A seeded random generator gives reproducible data: the same seed produces
byte-identical events, so tests and README numbers are repeatable.

Event shapes follow Stripe's envelope ({"id", "type", "created", "data": {"object"}}),
so the same code path also accepts real events from `stripe listen` in test mode.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import Engine

from ..ingest.event_handlers import processor_fee
from ..ingest.service import process_event


@dataclass(frozen=True)
class SimConfig:
    seed: int = 42
    start: date = date(2026, 1, 5)
    days: int = 14
    charges_per_day: int = 60
    refund_rate: float = 0.06


def _day_start_ts(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def build_events(cfg: SimConfig) -> list[dict]:
    """Return a deterministic list of events, ordered by time within each day."""
    rng = random.Random(cfg.seed)
    events: list[dict] = []
    seq = 0

    def next_evt() -> str:
        nonlocal seq
        seq += 1
        return f"evt_{seq:08d}"

    for day_index in range(cfg.days):
        d = cfg.start + timedelta(days=day_index)
        base = _day_start_ts(d)
        day_events: list[tuple[int, dict]] = []
        charges: list[tuple[str, int]] = []
        net_total = 0

        for n in range(cfg.charges_per_day):
            ts = base + rng.randint(0, 86_000)
            amount = rng.randint(500, 50_000)  # $5.00 - $500.00
            charge_id = f"ch_{rng.getrandbits(56):014x}"
            charges.append((charge_id, amount))
            net_total += amount - processor_fee(amount)
            customer = f"customer{day_index:02d}{n:03d}"
            day_events.append((ts, {
                "id": next_evt(), "type": "charge.succeeded", "created": ts,
                "data": {"object": {
                    "id": charge_id, "object": "charge", "amount": amount, "currency": "usd", "created": ts,
                    "receipt_email": f"{customer}@example.com",
                    "billing_details": {"email": f"{customer}@example.com", "name": f"Test Customer {customer}"},
                }},
            }))

        refund_count = round(cfg.charges_per_day * cfg.refund_rate)
        for charge_id, amount in rng.sample(charges, refund_count):
            ts = base + rng.randint(0, 86_000)
            refund_amount = amount if rng.random() < 0.5 else rng.randint(100, amount)
            net_total -= refund_amount
            day_events.append((ts, {
                "id": next_evt(), "type": "refund.created", "created": ts,
                "data": {"object": {
                    "id": f"re_{rng.getrandbits(56):014x}", "object": "refund", "amount": refund_amount,
                    "currency": "usd", "charge": charge_id, "created": ts,
                }},
            }))

        if net_total > 0:  # one payout at the end of each day
            ts = base + 86_399
            day_events.append((ts, {
                "id": next_evt(), "type": "payout.paid", "created": ts,
                "data": {"object": {
                    "id": f"po_{rng.getrandbits(56):014x}", "object": "payout", "amount": net_total,
                    "currency": "usd", "arrival_date": ts, "created": ts,
                }},
            }))

        day_events.sort(key=lambda pair: (pair[0], pair[1]["id"]))
        events.extend(event for _, event in day_events)

    return events


def post_events(engine: Engine, events: list[dict], pii_key: bytes | None = None) -> Counter:
    """Send events through the real processing path (no HTTP) and count the outcomes."""
    outcomes: Counter = Counter()
    for event in events:
        outcomes[process_event(engine, event, pii_key)] += 1
    return outcomes
