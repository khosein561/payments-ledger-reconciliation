"""
End-to-end proof: the reconciler finds EXACTLY the discrepancies we planted.

Pipeline under test:
    fake events -> ledger -> settlement files with planted errors -> load -> reconcile -> score
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import text

from conftest import PII_KEY, count
from ledger.generators.settlement_files import generate_settlement_files, load_plan
from ledger.generators.simulate import SimConfig, build_events, post_events
from ledger.pipeline import load_settlement, reconcile_date
from ledger.recon.evaluate import planted_breaks, score_against_plan
from ledger.recon.match import run_range

CFG = SimConfig(seed=7, start=date(2026, 1, 5), days=6, charges_per_day=30)
END = CFG.start + timedelta(days=CFG.days - 1)


@pytest.fixture()
def world(db, tmp_path):
    """Ledger populated, files generated, files loaded. Returns (plan, directory)."""
    post_events(db, build_events(CFG), PII_KEY)
    with db.begin() as conn:
        plan = generate_settlement_files(conn, CFG.start, END, tmp_path, seed=CFG.seed)
    for i in range(CFG.days):
        load_settlement(db, CFG.start + timedelta(days=i), tmp_path)
    return plan, tmp_path


def reconcile_all(db):
    with db.begin() as conn:
        run_range(conn, CFG.start, END)


def test_all_planted_breaks_found_and_no_false_breaks(db, world):
    plan, _ = world
    reconcile_all(db)
    with db.connect() as conn:
        score = score_against_plan(conn, plan, CFG.start, END)
    total = score.iloc[-1]
    assert total["planted"] == len(plan["planted"]) == 19
    assert total["missed"] == 0, score.to_string()
    assert total["false_positive"] == 0, score.to_string()
    assert (score.iloc[:-1]["planted"] == score.iloc[:-1]["found"]).all()


def test_every_break_type_is_exercised(world):
    plan, _ = world
    kinds = {p["break_type"] for p in plan["planted"]}
    assert kinds == {"missing_in_ledger", "missing_in_processor", "missing_at_bank",
                     "duplicate", "amount_mismatch", "timing_difference"}


def test_clean_data_produces_zero_breaks(db, tmp_path):
    post_events(db, build_events(CFG), PII_KEY)
    zero = {k: 0 for k in ("missing_in_processor", "duplicate", "amount_mismatch", "timing_difference",
                           "missing_in_ledger", "missing_at_bank")}
    with db.begin() as conn:
        plan = generate_settlement_files(conn, CFG.start, END, tmp_path, seed=CFG.seed, counts=zero)
    assert plan["planted"] == []
    for i in range(CFG.days):
        load_settlement(db, CFG.start + timedelta(days=i), tmp_path)
    reconcile_all(db)
    assert count(db, "recon_breaks") == 0
    assert count(db, "recon_matches", "match_pass = 'exact'") > 0
    assert count(db, "recon_matches", "match_pass = 'tolerant'") == 0


def test_rerunning_reconciliation_replaces_results_instead_of_duplicating(db, world):
    reconcile_all(db)
    breaks_once, matches_once = count(db, "recon_breaks"), count(db, "recon_matches")
    reconcile_all(db)
    reconcile_all(db)
    assert count(db, "recon_runs") == CFG.days
    assert count(db, "recon_breaks") == breaks_once
    assert count(db, "recon_matches") == matches_once


def test_loading_the_same_file_twice_adds_nothing(db, world):
    _, directory = world
    rows_before = count(db, "processor_settlement")
    result = load_settlement(db, CFG.start, directory)
    assert result == {"processor_rows_loaded": 0, "bank_rows_loaded": 0}
    assert count(db, "processor_settlement") == rows_before


def test_same_seed_gives_identical_plan(db, tmp_path):
    post_events(db, build_events(CFG), PII_KEY)
    with db.begin() as conn:
        a = generate_settlement_files(conn, CFG.start, END, tmp_path / "a", seed=1)
        b = generate_settlement_files(conn, CFG.start, END, tmp_path / "b", seed=1)
        c = generate_settlement_files(conn, CFG.start, END, tmp_path / "c", seed=2)
    assert planted_breaks(a) == planted_breaks(b)
    assert planted_breaks(a) != planted_breaks(c)
    assert (tmp_path / "a" / "processor_2026-01-05.csv").read_text() == (tmp_path / "b" / "processor_2026-01-05.csv").read_text()


def test_reversed_charge_disappears_from_ledger_side(db, world):
    """If we reverse a charge, the processor still reporting it should surface as MISSING_IN_LEDGER."""
    from ledger.core.posting import reverse_entry

    with db.begin() as conn:
        row = conn.execute(text(
            "SELECT t.entry_id, t.external_ref, t.business_date FROM ledger_transactions t "
            "WHERE entry_type = 'charge' AND external_ref NOT IN "
            "  (SELECT ref FROM recon_breaks) ORDER BY entry_id LIMIT 1")).fetchone()
        reverse_entry(conn, row.entry_id, "chargeback")
    reconcile_date(db, row.business_date)
    with db.connect() as conn:
        found = conn.execute(text(
            "SELECT 1 FROM recon_breaks WHERE ref = :r AND break_type = 'missing_in_ledger'"), {"r": row.external_ref}).fetchone()
    assert found is not None
