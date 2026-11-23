"""Pure-Python unit tests (no database): signatures, PII, fees, classification, matching."""

from datetime import date

import pytest

from ledger.ingest.event_handlers import processor_fee
from ledger.ingest.signature import SignatureError, compute_signature, make_signature_header, verify_stripe_signature
from ledger.recon.classify import BreakType, age_bucket, classify_break, classify_payout_break
from ledger.recon.match import match_payouts, match_transactions
from ledger.security.pii import mask_email, redact_payload, tokenize

SECRET = "whsec_unit"
BODY = b'{"id":"evt_1"}'


# ----------------------------------------------------------------- signature
def test_valid_signature_accepted():
    verify_stripe_signature(BODY, make_signature_header(BODY, SECRET, 1000), SECRET, now=1000)


def test_signature_with_two_v1_values_accepted_if_one_matches():
    good = compute_signature(BODY, SECRET, 1000)
    verify_stripe_signature(BODY, f"t=1000,v1=deadbeef,v1={good}", SECRET, now=1000)


@pytest.mark.parametrize("header", ["", "garbage", "t=abc,v1=x", "t=1000", "v1=abc"])
def test_malformed_headers_rejected(header):
    with pytest.raises(SignatureError):
        verify_stripe_signature(BODY, header, SECRET, now=1000)


def test_wrong_secret_wrong_body_and_old_timestamp_rejected():
    header = make_signature_header(BODY, SECRET, 1000)
    with pytest.raises(SignatureError, match="does not match"):
        verify_stripe_signature(BODY, header, "whsec_other", now=1000)
    with pytest.raises(SignatureError, match="does not match"):
        verify_stripe_signature(b'{"id":"evt_2"}', header, SECRET, now=1000)
    with pytest.raises(SignatureError, match="tolerance"):
        verify_stripe_signature(BODY, header, SECRET, now=1000 + 301)


# ----------------------------------------------------------------------- PII
def test_mask_email():
    assert mask_email("alice@example.com") == "a***@example.com"
    assert mask_email("not-an-email") == "***"
    assert mask_email("@example.com") == "***"
    assert mask_email(None) == "***"


def test_tokenize_is_deterministic_and_key_dependent():
    assert tokenize("alice", b"k1") == tokenize("alice", b"k1")
    assert tokenize("alice", b"k1") != tokenize("alice", b"k2")
    assert tokenize("alice", b"k1") != tokenize("bob", b"k1")
    assert tokenize("alice", b"k1").startswith("tok_")


def test_redact_payload_replaces_pii_keeps_business_fields_and_does_not_mutate():
    original = {"id": "evt_1", "data": {"amount": 500, "billing_details": {
        "email": "a@b.com", "name": "Al", "address": {"city": "Seattle"}}}, "items": [{"phone": "555"}]}
    snapshot = repr(original)
    out = redact_payload(original, b"k")
    assert repr(original) == snapshot
    assert out["data"]["amount"] == 500 and out["id"] == "evt_1"
    assert out["data"]["billing_details"]["email"] == "a***@b.com"
    assert out["data"]["billing_details"]["name"].startswith("tok_")
    assert out["data"]["billing_details"]["address"].startswith("tok_")
    assert out["items"][0]["phone"].startswith("tok_")


# ----------------------------------------------------------------------- fees
@pytest.mark.parametrize("amount,fee", [(10_000, 320), (500, 45), (1, 30), (34, 31), (100_000, 2930)])
def test_processor_fee_is_2_9_percent_plus_30_cents_half_up(amount, fee):
    assert processor_fee(amount) == fee


# -------------------------------------------------------------- classification
def led(ref="ch_1", gross=1000, fee=59, d=date(2026, 1, 5), typ="charge"):
    return {"entry_id": 1, "entry_type": typ, "external_ref": ref, "gross_minor": gross, "fee_minor": fee, "business_date": d}


def proc(ref="ch_1", gross=1000, fee=59, settled=date(2026, 1, 5), typ="charge"):
    return {"txn_id": ref, "type": typ, "gross_minor": gross, "fee_minor": fee, "net_minor": gross - fee,
            "txn_date": date(2026, 1, 5), "settled_on": settled}


def test_classify_agreement_and_each_break_type():
    assert classify_break(led(), proc()) is None
    assert classify_break(led(), None) is BreakType.MISSING_IN_PROCESSOR
    assert classify_break(None, proc()) is BreakType.MISSING_IN_LEDGER
    assert classify_break(led(), proc(gross=1001)) is BreakType.AMOUNT_MISMATCH
    assert classify_break(led(), proc(fee=60)) is BreakType.AMOUNT_MISMATCH
    assert classify_break(led(typ="charge"), proc(typ="refund")) is BreakType.AMOUNT_MISMATCH
    assert classify_break(led(), proc(settled=date(2026, 1, 7))) is BreakType.TIMING_DIFFERENCE


def test_amount_problem_outranks_timing_problem():
    assert classify_break(led(), proc(gross=1001, settled=date(2026, 1, 7))) is BreakType.AMOUNT_MISMATCH


def test_classify_payouts():
    payout = led(ref="po_1", gross=900, fee=0, typ="payout")
    assert classify_payout_break(payout, {"amount_minor": 900}) is None
    assert classify_payout_break(payout, {"amount_minor": 901}) is BreakType.AMOUNT_MISMATCH
    assert classify_payout_break(payout, None) is BreakType.MISSING_AT_BANK
    assert classify_payout_break(None, {"amount_minor": 1}) is BreakType.MISSING_IN_LEDGER


@pytest.mark.parametrize("days,bucket", [(0, "0-1d"), (1, "0-1d"), (2, "2-3d"), (3, "2-3d"), (4, "4-7d"),
                                          (7, "4-7d"), (8, "8d+"), (400, "8d+")])
def test_age_buckets(days, bucket):
    assert age_bucket(days) == bucket


def test_negative_age_rejected():
    with pytest.raises(ValueError):
        age_bucket(-1)


# -------------------------------------------------------------------- matching
D = date(2026, 1, 5)


def types(result):
    return sorted((b.break_type.value, b.ref) for b in result.breaks)


def test_perfect_data_produces_only_exact_matches():
    r = match_transactions([led("a"), led("b")], [proc("a"), proc("b")], D)
    assert [m.match_pass for m in r.matches] == ["exact", "exact"] and not r.breaks


def test_each_problem_is_found_once():
    ledger = [led("ok"), led("gone"), led("amt"), led("late"), led("dup")]
    processor = [proc("ok"), proc("amt", gross=1500), proc("late", settled=date(2026, 1, 7)), proc("dup"), proc("dup"),
                 proc("ghost")]
    r = match_transactions(ledger, processor, D)
    assert types(r) == [("amount_mismatch", "amt"), ("duplicate", "dup"), ("missing_in_ledger", "ghost"),
                        ("missing_in_processor", "gone"), ("timing_difference", "late")]


def test_timing_within_tolerance_is_matched_tolerantly_beyond_it_is_not():
    near = match_transactions([led("a")], [proc("a", settled=date(2026, 1, 8))], D, tolerance_days=3)
    assert [m.match_pass for m in near.matches] == ["tolerant"]
    assert types(near) == [("timing_difference", "a")]

    far = match_transactions([led("a")], [proc("a", settled=date(2026, 1, 20))], D, tolerance_days=3)
    assert far.matches == [] and types(far) == [("timing_difference", "a")]


def test_empty_inputs_are_fine():
    r = match_transactions([], [], D)
    assert r.matches == [] and r.breaks == []


def test_payout_matching():
    ledger = [led("po_ok", 500, 0, typ="payout"), led("po_gone", 700, 0, typ="payout"), led("po_amt", 100, 0, typ="payout")]
    bank = [{"bank_txn_id": "BK1", "reference": "po_ok", "amount_minor": 500},
            {"bank_txn_id": "BK2", "reference": "po_amt", "amount_minor": 101},
            {"bank_txn_id": "BK3", "reference": "po_surprise", "amount_minor": 5}]
    r = match_payouts(ledger, bank, D)
    assert types(r) == [("amount_mismatch", "po_amt"), ("missing_at_bank", "po_gone"), ("missing_in_ledger", "po_surprise")]
