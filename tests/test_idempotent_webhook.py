"""Webhook safety: signatures, duplicates, poison events, outages, privacy."""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from conftest import PII_KEY, WEBHOOK_SECRET, count
from helpers import JAN_5, charge_event, signed_post
from ledger.core.accounts import seed_accounts
from ledger.ingest.service import replay_dead_letter
from ledger.ingest.signature import make_signature_header
from ledger.ingest.webhook_app import create_app


@pytest.fixture()
def client(db):
    return TestClient(create_app(engine=db, webhook_secret=WEBHOOK_SECRET, pii_key=PII_KEY))


def test_same_event_twice_posts_once(client, db):
    event = charge_event()
    first = signed_post(client, event)
    second = signed_post(client, event)
    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == "processed"
    assert second.json()["status"] == "duplicate"
    assert count(db, "journal_entries") == 1
    assert count(db, "webhook_events") == 1


def test_five_deliveries_still_one_entry(client, db):
    event = charge_event(evt="evt_five")
    statuses = [signed_post(client, event).json()["status"] for _ in range(5)]
    assert statuses == ["processed"] + ["duplicate"] * 4
    assert count(db, "journal_entries") == 1


def test_charge_posts_a_balanced_four_line_entry(client, db):
    signed_post(client, charge_event(amount=10_000))
    with db.connect() as conn:
        rows = conn.execute(text(
            "SELECT a.code, p.direction, p.amount_minor FROM postings p JOIN accounts a USING (account_id) "
            "ORDER BY p.posting_id")).fetchall()
    assert [tuple(r) for r in rows] == [
        ("PROCESSOR_CLEARING", "debit", 10_000), ("REVENUE", "credit", 10_000),
        ("FEES_EXPENSE", "debit", 320), ("PROCESSOR_CLEARING", "credit", 320)]


def test_bad_signature_rejected_and_nothing_stored(client, db):
    resp = signed_post(client, charge_event(), secret="whsec_wrong")
    assert resp.status_code == 400
    assert count(db, "webhook_events") == 0
    assert count(db, "journal_entries") == 0


def test_missing_signature_header_rejected(client, db):
    assert signed_post(client, charge_event(), header=False).status_code == 400
    assert count(db, "webhook_events") == 0


def test_tampered_body_rejected(client, db):
    body = json.dumps(charge_event()).encode()
    header = make_signature_header(body, WEBHOOK_SECRET)
    tampered = body.replace(b"10000", b"99999")
    assert signed_post(client, tampered, header=header).status_code == 400
    assert count(db, "journal_entries") == 0


def test_stale_timestamp_rejected_as_possible_replay(client, db):
    assert signed_post(client, charge_event(), timestamp=1_000_000).status_code == 400


def test_malformed_json_with_valid_signature_is_400(client, db):
    assert signed_post(client, b"{not json").status_code == 400
    assert signed_post(client, json.dumps({"no": "id"}).encode()).status_code == 400


def test_unknown_event_type_is_acknowledged_and_ignored(client, db):
    event = {"id": "evt_x", "type": "customer.created", "created": JAN_5, "data": {"object": {}}}
    resp = signed_post(client, event)
    assert resp.status_code == 200 and resp.json()["status"] == "ignored"
    assert count(db, "journal_entries") == 0
    assert count(db, "webhook_events", "status = 'ignored'") == 1


def test_unsupported_currency_goes_to_dead_letter_and_still_returns_200(client, db):
    resp = signed_post(client, charge_event(evt="evt_eur", currency="eur"))
    assert resp.status_code == 200 and resp.json()["status"] == "dead_letter"
    assert count(db, "journal_entries") == 0
    assert count(db, "dead_letter_events", "resolved_at IS NULL") == 1
    assert count(db, "webhook_events", "status = 'dead_letter'") == 1
    # Stripe redelivering the same poison event must not create more work.
    assert signed_post(client, charge_event(evt="evt_eur", currency="eur")).json()["status"] == "duplicate"
    assert count(db, "dead_letter_events") == 1


def test_float_amount_event_is_dead_lettered_not_posted(client, db):
    resp = signed_post(client, charge_event(evt="evt_float", amount=10.5))
    assert resp.json()["status"] == "dead_letter"
    assert count(db, "journal_entries") == 0


def test_dead_letter_can_be_replayed_after_the_cause_is_fixed(client, db):
    with db.begin() as conn:
        conn.execute(text("DELETE FROM accounts"))  # simulate a broken deployment: no chart of accounts
    assert signed_post(client, charge_event(evt="evt_fixme")).json()["status"] == "dead_letter"
    assert count(db, "journal_entries") == 0

    with db.begin() as conn:
        seed_accounts(conn)  # fix the cause
    with db.connect() as conn:
        dl_id = conn.execute(text("SELECT id FROM dead_letter_events")).scalar_one()
    assert replay_dead_letter(db, dl_id) == "processed"
    assert count(db, "journal_entries") == 1
    assert count(db, "dead_letter_events", "resolved_at IS NOT NULL") == 1
    assert count(db, "webhook_events", "status = 'processed'") == 1


def test_personal_data_is_never_stored(client, db):
    signed_post(client, charge_event(email="alice@example.com", name="Alice Example"))
    with db.connect() as conn:
        stored = conn.execute(text("SELECT payload::text FROM webhook_events")).scalar_one()
    assert "alice@example.com" not in stored
    assert "Alice Example" not in stored
    assert "a***@example.com" in stored
    assert "tok_" in stored


def test_healthz_ok(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_database_outage_returns_503_so_the_sender_retries_then_recovers(db):
    dead_engine = create_engine("postgresql+psycopg2://nobody:x@127.0.0.1:1/none", connect_args={"connect_timeout": 1})
    down = TestClient(create_app(engine=dead_engine, webhook_secret=WEBHOOK_SECRET, pii_key=PII_KEY))
    event = charge_event(evt="evt_outage")

    assert signed_post(down, event).status_code == 503
    assert down.get("/healthz").status_code == 503

    up = TestClient(create_app(engine=db, webhook_secret=WEBHOOK_SECRET, pii_key=PII_KEY))
    assert signed_post(up, event).json()["status"] == "processed"  # the retry succeeds, exactly once
    assert count(db, "journal_entries") == 1
