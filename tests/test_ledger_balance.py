"""Ledger invariants: the rules that keep the books trustworthy."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from conftest import count
from ledger.core.posting import LedgerError, Line, create_entry, reverse_entry

D = date(2026, 1, 5)
GOOD = [Line("PROCESSOR_CLEARING", "debit", 1000), Line("REVENUE", "credit", 1000)]


def post(engine, sid="evt_a", lines=GOOD, etype="charge", ref="ch_a"):
    with engine.begin() as conn:
        return create_entry(conn, sid, etype, "test", lines, D, external_ref=ref)


def test_unbalanced_entry_rejected_and_nothing_written(db):
    bad = [Line("PROCESSOR_CLEARING", "debit", 1000), Line("REVENUE", "credit", 999)]
    with pytest.raises(LedgerError, match="not balanced"):
        post(db, lines=bad)
    assert count(db, "journal_entries") == 0
    assert count(db, "postings") == 0


def test_database_itself_rejects_unbalanced_entry_even_if_python_is_bypassed(db):
    """Belt and braces: raw SQL that skips our Python validation still cannot unbalance the books."""
    with pytest.raises(DBAPIError, match="not balanced"):
        with db.begin() as conn:
            conn.execute(text("INSERT INTO journal_entries (source_event_id, entry_type, description, business_date) "
                              "VALUES ('raw1', 'adjustment', 'x', '2026-01-05')"))
            conn.execute(text("INSERT INTO postings (entry_id, account_id, direction, amount_minor) "
                              "SELECT currval('journal_entries_entry_id_seq'), account_id, 'debit', 500 FROM accounts WHERE code='CASH'"))
    assert count(db, "journal_entries") == 0  # the whole transaction rolled back


def test_entry_header_without_postings_rejected_at_commit(db):
    with pytest.raises(DBAPIError, match="no postings"):
        with db.begin() as conn:
            conn.execute(text("INSERT INTO journal_entries (source_event_id, entry_type, description, business_date) "
                              "VALUES ('empty1', 'adjustment', 'x', '2026-01-05')"))


@pytest.mark.parametrize("bad_amount", [10.50, Decimal("10.50"), "1000", True, None])
def test_non_integer_amount_rejected(db, bad_amount):
    lines = [Line("PROCESSOR_CLEARING", "debit", bad_amount), Line("REVENUE", "credit", bad_amount)]
    with pytest.raises(LedgerError, match="int number of minor units"):
        post(db, lines=lines)


def test_integer_minor_units_accepted(db):
    result = post(db)
    assert result.created is True
    assert count(db, "postings") == 2


@pytest.mark.parametrize("amount", [0, -5])
def test_zero_and_negative_amounts_rejected(db, amount):
    with pytest.raises(LedgerError, match="positive"):
        post(db, lines=[Line("CASH", "debit", amount), Line("REVENUE", "credit", amount)])


def test_each_currency_must_balance_on_its_own(db):
    lines = [Line("CASH", "debit", 100, "USD"), Line("REVENUE", "credit", 100, "EUR")]
    with pytest.raises(LedgerError, match="not balanced"):
        post(db, lines=lines)


def test_ledger_is_append_only(db):
    post(db)
    for sql in (
        "UPDATE postings SET amount_minor = 1",
        "DELETE FROM postings",
        "UPDATE journal_entries SET description = 'tampered'",
        "DELETE FROM journal_entries",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            with db.begin() as conn:
                conn.execute(text(sql))
    assert count(db, "postings") == 2


def test_posting_same_event_twice_writes_once(db):
    first = post(db)
    second = post(db)
    assert first.created and not second.created
    assert first.entry_id == second.entry_id
    assert count(db, "journal_entries") == 1
    assert count(db, "postings") == 2


def test_failure_midway_rolls_back_everything(db):
    lines = [Line("PROCESSOR_CLEARING", "debit", 1000), Line("NO_SUCH_ACCOUNT", "credit", 1000)]
    with pytest.raises(KeyError):
        post(db, lines=lines)
    assert count(db, "journal_entries") == 0  # the header insert was rolled back with the rest


def test_reverse_entry_mirrors_original_and_hides_both_from_reconciliation(db):
    original = post(db)
    with db.begin() as conn:
        rev = reverse_entry(conn, original.entry_id, "customer dispute")
    assert rev.created
    with db.connect() as conn:
        sums = conn.execute(text(
            "SELECT direction, SUM(amount_minor) FROM postings GROUP BY direction ORDER BY direction")).fetchall()
        assert [tuple(r) for r in sums] == [("credit", 2000), ("debit", 2000)]
        assert conn.execute(text("SELECT count(*) FROM ledger_transactions")).scalar_one() == 0


def test_reversing_twice_is_a_no_op_and_reversals_cannot_be_reversed(db):
    original = post(db)
    with db.begin() as conn:
        first = reverse_entry(conn, original.entry_id, "r")
        again = reverse_entry(conn, original.entry_id, "r")
    assert not again.created and again.entry_id == first.entry_id
    with pytest.raises(LedgerError, match="reverse a reversal"):
        with db.begin() as conn:
            reverse_entry(conn, first.entry_id, "oops")


def test_unknown_entry_type_and_direction_rejected(db):
    with pytest.raises(LedgerError):
        post(db, etype="bogus")
    with pytest.raises(LedgerError, match="direction"):
        post(db, lines=[Line("CASH", "sideways", 5), Line("REVENUE", "credit", 5)])
