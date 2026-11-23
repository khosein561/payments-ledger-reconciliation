"""The daily pipeline, its data-quality gate, report, alerting and database roles."""

from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError

import ledger.pipeline as pipeline
from conftest import PII_KEY
from ledger.dq import DataQualityError, assert_dq, check_dead_letters, check_webhook_freshness, run_dq_checks
from ledger.generators.settlement_files import generate_settlement_files
from ledger.generators.simulate import SimConfig, build_events, post_events
from ledger.recon.report import build_exception_report, write_report

CFG = SimConfig(seed=11, start=date(2026, 2, 2), days=3, charges_per_day=25)
END = CFG.start + timedelta(days=CFG.days - 1)


@pytest.fixture()
def staged(db, tmp_path):
    post_events(db, build_events(CFG), PII_KEY)
    with db.begin() as conn:
        generate_settlement_files(conn, CFG.start, END, tmp_path, seed=CFG.seed,
                                  counts={"missing_in_processor": 1, "duplicate": 1, "amount_mismatch": 1,
                                          "timing_difference": 1, "missing_in_ledger": 1, "missing_at_bank": 1})
    return tmp_path


def test_run_daily_end_to_end_writes_report_and_alerts(db, staged, tmp_path, monkeypatch):
    alerts = []
    monkeypatch.setattr(pipeline, "notify", alerts.append)
    summary = pipeline.run_daily(db, CFG.start, staged, tmp_path / "reports")
    assert summary["run_id"] > 0
    assert summary["processor_rows_loaded"] > 0
    assert (tmp_path / "reports" / f"exceptions_{CFG.start.isoformat()}.csv").exists()
    # Alerting only fires when high-severity breaks exist for that day's report.
    assert bool(alerts) == (summary["high_severity"] >= 1)


def test_run_daily_twice_is_safe(db, staged, tmp_path):
    a = pipeline.run_daily(db, CFG.start, staged, tmp_path / "r")
    b = pipeline.run_daily(db, CFG.start, staged, tmp_path / "r")
    assert b["processor_rows_loaded"] == 0 and b["bank_rows_loaded"] == 0
    assert a["open_breaks"] == b["open_breaks"]


def test_missing_file_fails_loudly(db, tmp_path):
    with pytest.raises(FileNotFoundError):
        pipeline.load_settlement(db, CFG.start, tmp_path)
    assert not pipeline.files_ready(tmp_path, CFG.start)


def test_dq_passes_on_healthy_ledger(db, staged):
    with db.connect() as conn:
        results = run_dq_checks(conn)
    assert all(r.passed for r in results if r.severity == "error")


def test_dq_gate_blocks_pipeline_when_ledger_is_unbalanced(db, staged, tmp_path):
    """Force a corrupt ledger (by disabling the DB trigger, as an attacker or bug might) and prove we stop."""
    with db.begin() as conn:
        conn.execute(text("ALTER TABLE postings DISABLE TRIGGER trg_postings_balanced"))
        conn.execute(text("ALTER TABLE journal_entries DISABLE TRIGGER trg_entries_balanced"))
        conn.execute(text("INSERT INTO postings (entry_id, account_id, direction, amount_minor) "
                          "SELECT (SELECT min(entry_id) FROM journal_entries), account_id, 'debit', 1 "
                          "FROM accounts WHERE code = 'CASH'"))
        conn.execute(text("ALTER TABLE postings ENABLE TRIGGER trg_postings_balanced"))
        conn.execute(text("ALTER TABLE journal_entries ENABLE TRIGGER trg_entries_balanced"))
    with db.connect() as conn:
        with pytest.raises(DataQualityError, match="ledger_sums_to_zero"):
            assert_dq(conn)
    with pytest.raises(DataQualityError):
        pipeline.run_daily(db, CFG.start, staged, tmp_path / "r")


def test_dq_warnings_do_not_block(db):
    with db.begin() as conn:
        conn.execute(text("INSERT INTO dead_letter_events (event_id, payload, error) VALUES ('e', '{}', 'boom')"))
    with db.connect() as conn:
        assert not check_dead_letters(conn).passed
        assert_dq(conn)  # warn-level only: must not raise
        assert not check_webhook_freshness(conn, max_age_hours=1).passed  # no events at all


def test_report_ordering_and_columns(db, staged):
    pipeline.load_settlement(db, CFG.start, staged)
    pipeline.reconcile_date(db, CFG.start)
    with db.connect() as conn:
        df = build_exception_report(conn, as_of=CFG.start + timedelta(days=10))
    assert list(df.columns)[:3] == ["break_type", "ref", "amount_minor"]
    assert df["age_days"].is_monotonic_decreasing
    assert set(df["age_bucket"]) <= {"0-1d", "2-3d", "4-7d", "8d+"}


def test_empty_report_is_valid(db, tmp_path):
    with db.connect() as conn:
        df = build_exception_report(conn, as_of=date(2026, 1, 1))
    paths = write_report(df, tmp_path, date(2026, 1, 1))
    assert "Open breaks: **0**" in paths["markdown"].read_text()


# ----------------------------------------------------------------- database roles
def as_role(db, role, sql):
    with db.connect() as conn:
        conn.execute(text(f"SET ROLE {role}"))
        try:
            return conn.execute(text(sql)).fetchall()
        finally:
            conn.rollback()


def test_analyst_can_read_views_but_not_write_or_see_raw_events(db, staged):
    assert as_role(db, "analyst_ro", "SELECT count(*) FROM ledger_transactions")
    assert as_role(db, "analyst_ro", "SELECT count(*) FROM analyst_webhook_events")
    for sql in ("SELECT * FROM webhook_events", "INSERT INTO accounts (code, name, type) VALUES ('X','x','asset')",
                "SELECT * FROM postings"):
        with pytest.raises(ProgrammingError, match="permission denied"):
            as_role(db, "analyst_ro", sql)


def test_ledger_writer_can_insert_but_not_change_or_delete_history(db):
    for sql in ("UPDATE postings SET amount_minor = 1", "DELETE FROM journal_entries", "TRUNCATE postings"):
        with pytest.raises(ProgrammingError, match="permission denied"):
            as_role(db, "ledger_writer", sql)


def test_recon_job_cannot_write_to_the_ledger(db):
    with pytest.raises(ProgrammingError, match="permission denied"):
        as_role(db, "recon_job", "INSERT INTO journal_entries (source_event_id, entry_type, description, business_date) "
                                 "VALUES ('z','adjustment','z','2026-01-01')")
