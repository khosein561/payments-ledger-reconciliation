"""Small builders used by several test files."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from ledger.ingest.signature import make_signature_header

from conftest import WEBHOOK_SECRET

JAN_5 = int(datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc).timestamp())


def charge_event(evt="evt_1", charge="ch_1", amount=10_000, currency="usd", created=JAN_5,
                 email="alice@example.com", name="Alice Example"):
    return {
        "id": evt, "type": "charge.succeeded", "created": created,
        "data": {"object": {
            "id": charge, "object": "charge", "amount": amount, "currency": currency, "created": created,
            "receipt_email": email,
            "billing_details": {"email": email, "name": name},
        }},
    }


def refund_event(evt="evt_r1", refund="re_1", amount=2_000, created=JAN_5):
    return {"id": evt, "type": "refund.created", "created": created,
            "data": {"object": {"id": refund, "object": "refund", "amount": amount, "currency": "usd", "created": created}}}


def payout_event(evt="evt_p1", payout="po_1", amount=7_000, created=JAN_5):
    return {"id": evt, "type": "payout.paid", "created": created,
            "data": {"object": {"id": payout, "object": "payout", "amount": amount, "currency": "usd",
                                "arrival_date": created, "created": created}}}


def signed_post(client, event_or_bytes, secret=WEBHOOK_SECRET, timestamp=None, header=None):
    body = event_or_bytes if isinstance(event_or_bytes, bytes) else json.dumps(event_or_bytes).encode()
    headers = {"Content-Type": "application/json"}
    if header is not False:
        headers["Stripe-Signature"] = header or make_signature_header(body, secret, timestamp)
    return client.post("/webhook", content=body, headers=headers)
