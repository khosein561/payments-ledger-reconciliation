"""
Break classification.

WHY THIS FILE EXISTS: "the numbers don't match" is not actionable. Operations teams
triage by TYPE (a missing webhook needs engineering; a timing difference usually just
needs waiting) and by AGE (a one-day-old break is normal; an eight-day-old one is an
escalation). This file turns a mismatch into a named type and an age bucket.

Pure functions, no database: easy to test exhaustively.
"""

from __future__ import annotations

from datetime import date
from enum import Enum


class BreakType(str, Enum):
    MISSING_IN_LEDGER = "missing_in_ledger"        # processor says it happened; we never recorded it
    MISSING_IN_PROCESSOR = "missing_in_processor"  # we recorded it; the processor has no record
    MISSING_AT_BANK = "missing_at_bank"            # payout recorded; no matching bank deposit
    DUPLICATE = "duplicate"                        # same transaction appears more than once
    AMOUNT_MISMATCH = "amount_mismatch"            # both sides agree it happened but not on the amount
    TIMING_DIFFERENCE = "timing_difference"        # same amount, settled on a different day


def classify_break(ledger_row: dict | None, processor_row: dict | None) -> BreakType | None:
    """Compare one ledger transaction with one processor row.

    Returns None when they agree completely. Decision order matters: existence first,
    then amounts, then dates (an amount problem is more serious than a date problem).
    """
    if ledger_row is not None and processor_row is None:
        return BreakType.MISSING_IN_PROCESSOR
    if processor_row is not None and ledger_row is None:
        return BreakType.MISSING_IN_LEDGER
    if ledger_row is None and processor_row is None:
        return None

    if (
        ledger_row["entry_type"] != processor_row["type"]
        or ledger_row["gross_minor"] != processor_row["gross_minor"]
        or ledger_row["fee_minor"] != processor_row["fee_minor"]
    ):
        return BreakType.AMOUNT_MISMATCH
    if ledger_row["business_date"] != processor_row["settled_on"]:
        return BreakType.TIMING_DIFFERENCE
    return None


def classify_payout_break(ledger_payout: dict | None, bank_row: dict | None) -> BreakType | None:
    """Compare one ledger payout with one bank deposit."""
    if ledger_payout is not None and bank_row is None:
        return BreakType.MISSING_AT_BANK
    if bank_row is not None and ledger_payout is None:
        return BreakType.MISSING_IN_LEDGER
    if ledger_payout is None and bank_row is None:
        return None
    if ledger_payout["gross_minor"] != bank_row["amount_minor"]:
        return BreakType.AMOUNT_MISMATCH
    return None


def age_bucket(days_open: int) -> str:
    """Bucket a break's age the way ops reports do."""
    if days_open < 0:
        raise ValueError("days_open cannot be negative")
    if days_open <= 1:
        return "0-1d"
    if days_open <= 3:
        return "2-3d"
    if days_open <= 7:
        return "4-7d"
    return "8d+"


def days_between(earlier: date, later: date) -> int:
    return (later - earlier).days
