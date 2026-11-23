"""
Three-way matching: ledger vs processor vs bank.

WHY THIS FILE EXISTS: this is the heart of the project. Each source of truth can be
wrong in its own way, so we compare them pairwise:

    ledger  <-->  processor settlement file     (per charge / refund, matched on txn id)
    ledger  <-->  bank statement                (per payout, matched on payout id)

Matching happens in passes, cheapest and strictest first:
  1. EXACT     same id, same type/gross/fee, settled on the same day.
  2. TOLERANT  same id and amounts, settled a few days away -> matched, but we still
               raise a low-severity TIMING_DIFFERENCE so the pattern is visible.
Anything left over becomes a break, classified by classify.py.

The matching functions are PURE (lists in, results out), so they can be unit-tested
without a database. `run_reconciliation` is the thin database wrapper around them.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import Connection, text

from ..db import audit
from .classify import BreakType, classify_break, classify_payout_break


@dataclass
class Break:
    break_type: BreakType
    ref: str
    amount_minor: int | None
    business_date: date
    notes: str = ""


@dataclass
class Match:
    ledger_entry_id: int | None
    processor_txn_id: str | None
    bank_txn_id: str | None
    match_pass: str  # 'exact' or 'tolerant'


@dataclass
class MatchResult:
    matches: list[Match] = field(default_factory=list)
    breaks: list[Break] = field(default_factory=list)


def match_transactions(
    ledger_rows: list[dict], processor_rows: list[dict], business_date: date, tolerance_days: int = 3
) -> MatchResult:
    """Match ledger charges/refunds against processor settlement rows."""
    result = MatchResult()

    # Group processor rows by transaction id. The first occurrence is the "real" one;
    # any repeats are duplicates.
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in processor_rows:
        grouped[row["txn_id"]].append(row)
    for txn_id, rows in grouped.items():
        for extra in rows[1:]:
            result.breaks.append(Break(BreakType.DUPLICATE, txn_id, extra["gross_minor"], business_date,
                                       f"appears {len(rows)} times in the processor file"))
    canonical = {txn_id: rows[0] for txn_id, rows in grouped.items()}

    seen_refs = set()
    for led in sorted(ledger_rows, key=lambda r: r["external_ref"]):
        ref = led["external_ref"]
        seen_refs.add(ref)
        proc = canonical.get(ref)
        kind = classify_break(led, proc)

        if kind is None:
            result.matches.append(Match(led["entry_id"], ref, None, "exact"))
        elif kind is BreakType.TIMING_DIFFERENCE:
            gap = abs((proc["settled_on"] - led["business_date"]).days)
            if gap <= tolerance_days:
                result.matches.append(Match(led["entry_id"], ref, None, "tolerant"))
            result.breaks.append(Break(kind, ref, led["gross_minor"], business_date,
                                       f"settled {gap} day(s) after the ledger date"))
        elif kind is BreakType.AMOUNT_MISMATCH:
            result.breaks.append(Break(
                kind, ref, led["gross_minor"], business_date,
                f"ledger {led['entry_type']} gross={led['gross_minor']} fee={led['fee_minor']}; "
                f"processor {proc['type']} gross={proc['gross_minor']} fee={proc['fee_minor']}"))
        else:  # MISSING_IN_PROCESSOR
            result.breaks.append(Break(kind, ref, led["gross_minor"], business_date,
                                       "in the ledger but not in the processor file"))

    for txn_id, proc in canonical.items():
        if txn_id not in seen_refs:
            result.breaks.append(Break(BreakType.MISSING_IN_LEDGER, txn_id, proc["gross_minor"], business_date,
                                       "in the processor file but not in the ledger (missed webhook?)"))
    return result


def match_payouts(ledger_payouts: list[dict], bank_rows: list[dict], business_date: date) -> MatchResult:
    """Match ledger payouts against bank deposits (by payout id)."""
    result = MatchResult()

    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in bank_rows:
        grouped[row["reference"]].append(row)
    for ref, rows in grouped.items():
        for extra in rows[1:]:
            result.breaks.append(Break(BreakType.DUPLICATE, ref, extra["amount_minor"], business_date,
                                       f"bank shows {len(rows)} deposits for this payout"))
    canonical = {ref: rows[0] for ref, rows in grouped.items()}

    seen = set()
    for led in sorted(ledger_payouts, key=lambda r: r["external_ref"]):
        ref = led["external_ref"]
        seen.add(ref)
        bank = canonical.get(ref)
        kind = classify_payout_break(led, bank)
        if kind is None:
            result.matches.append(Match(led["entry_id"], None, bank["bank_txn_id"], "exact"))
        elif kind is BreakType.AMOUNT_MISMATCH:
            result.breaks.append(Break(kind, ref, led["gross_minor"], business_date,
                                       f"payout {led['gross_minor']} vs bank deposit {bank['amount_minor']}"))
        else:  # MISSING_AT_BANK
            result.breaks.append(Break(kind, ref, led["gross_minor"], business_date,
                                       "payout recorded but no matching bank deposit"))

    for ref, bank in canonical.items():
        if ref not in seen:
            result.breaks.append(Break(BreakType.MISSING_IN_LEDGER, ref, bank["amount_minor"], business_date,
                                       "bank deposit with no matching payout in the ledger"))
    return result


def run_reconciliation(conn: Connection, business_date: date, tolerance_days: int = 3, actor: str = "recon_job") -> int:
    """Reconcile one business date and store the results. Returns the run id.

    Re-running the same date REPLACES the previous run (delete + insert inside the
    caller's transaction), so results never duplicate and a failure leaves the old
    run intact.
    """
    conn.execute(text("DELETE FROM recon_runs WHERE business_date = :d"), {"d": business_date})
    run_id = conn.execute(
        text("INSERT INTO recon_runs (business_date) VALUES (:d) RETURNING run_id"), {"d": business_date}
    ).scalar_one()

    ledger = [dict(r) for r in conn.execute(
        text("SELECT * FROM ledger_transactions WHERE business_date = :d ORDER BY external_ref"),
        {"d": business_date}).mappings().all()]
    transactions = [r for r in ledger if r["entry_type"] in ("charge", "refund")]
    payouts = [r for r in ledger if r["entry_type"] == "payout"]

    processor = [dict(r) for r in conn.execute(
        text("SELECT * FROM processor_settlement WHERE txn_date = :d ORDER BY id"), {"d": business_date}).mappings().all()]
    bank = [dict(r) for r in conn.execute(
        text("SELECT * FROM bank_settlement WHERE value_date = :d ORDER BY id"), {"d": business_date}).mappings().all()]

    tx_result = match_transactions(transactions, processor, business_date, tolerance_days)
    payout_result = match_payouts(payouts, bank, business_date)
    matches = tx_result.matches + payout_result.matches
    breaks = tx_result.breaks + payout_result.breaks

    if matches:
        conn.execute(
            text(
                """
                INSERT INTO recon_matches (run_id, ledger_entry_id, processor_txn_id, bank_txn_id, match_pass)
                VALUES (:run_id, :lid, :pid, :bid, :pass)
                """
            ),
            [{"run_id": run_id, "lid": m.ledger_entry_id, "pid": m.processor_txn_id,
              "bid": m.bank_txn_id, "pass": m.match_pass} for m in matches],
        )
    if breaks:
        conn.execute(
            text(
                """
                INSERT INTO recon_breaks (run_id, break_type, ref, amount_minor, business_date, first_seen_on, notes)
                VALUES (:run_id, :bt, :ref, :amt, :bd, :bd, :notes)
                """
            ),
            [{"run_id": run_id, "bt": b.break_type.value, "ref": b.ref, "amt": b.amount_minor,
              "bd": b.business_date, "notes": b.notes} for b in breaks],
        )

    conn.execute(
        text(
            """
            UPDATE recon_runs
               SET finished_at = now(), status = 'completed', ledger_count = :lc, processor_count = :pc,
                   bank_count = :bc, matched_count = :mc, break_count = :kc
             WHERE run_id = :id
            """
        ),
        {"lc": len(ledger), "pc": len(processor), "bc": len(bank), "mc": len(matches), "kc": len(breaks), "id": run_id},
    )
    audit(conn, actor, "reconcile", "recon_run", run_id, after={"date": business_date, "breaks": len(breaks)})
    return int(run_id)


def run_range(conn: Connection, start: date, end: date, tolerance_days: int = 3) -> list[int]:
    """Reconcile every date in [start, end]."""
    from datetime import timedelta

    run_ids = []
    for i in range((end - start).days + 1):
        run_ids.append(run_reconciliation(conn, start + timedelta(days=i), tolerance_days))
    return run_ids
