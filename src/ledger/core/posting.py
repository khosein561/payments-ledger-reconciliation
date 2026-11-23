"""
Posting: the ONLY way money enters the ledger.

WHY THIS FILE EXISTS: every money movement in the system goes through `create_entry`.
Funnelling writes through one function means the rules (integer amounts, balanced
entries, idempotency, audit) are enforced in one place. The database enforces them
too (see db/migrations/001_ledger_core.sql), so this is belt AND braces: Python gives
a friendly error early, Postgres guarantees correctness even if someone bypasses
this code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import NamedTuple

from sqlalchemy import Connection, text

from ..db import audit
from .accounts import get_account_id

VALID_DIRECTIONS = ("debit", "credit")
VALID_ENTRY_TYPES = ("charge", "refund", "payout", "reversal", "adjustment")


class LedgerError(ValueError):
    """The requested entry is invalid (unbalanced, wrong type, bad amount...)."""


@dataclass(frozen=True)
class Line:
    """One side of a journal entry."""

    account_code: str
    direction: str  # 'debit' or 'credit'
    amount_minor: int  # integer cents; never a float
    currency: str = "USD"


class EntryResult(NamedTuple):
    entry_id: int
    created: bool  # False means "this event was already posted; nothing was written"


def validate_lines(lines: list[Line]) -> None:
    """Reject anything that could corrupt the books BEFORE touching the database."""
    if len(lines) < 2:
        raise LedgerError("an entry needs at least two lines")

    totals: dict[str, int] = {}
    for ln in lines:
        # bool is a subclass of int in Python, so True would sneak through without this check.
        if isinstance(ln.amount_minor, bool) or not isinstance(ln.amount_minor, int):
            raise LedgerError(
                f"amount must be an int number of minor units (cents), got {type(ln.amount_minor).__name__}: "
                f"{ln.amount_minor!r}. Floats are rejected on purpose: 0.1 + 0.2 != 0.3."
            )
        if ln.amount_minor <= 0:
            raise LedgerError("amount must be positive; use the direction to say debit or credit")
        if ln.direction not in VALID_DIRECTIONS:
            raise LedgerError(f"direction must be one of {VALID_DIRECTIONS}, got {ln.direction!r}")
        if not (isinstance(ln.currency, str) and len(ln.currency) == 3):
            raise LedgerError(f"currency must be a 3-letter code, got {ln.currency!r}")
        signed = ln.amount_minor if ln.direction == "debit" else -ln.amount_minor
        totals[ln.currency] = totals.get(ln.currency, 0) + signed

    unbalanced = {cur: diff for cur, diff in totals.items() if diff != 0}
    if unbalanced:
        raise LedgerError(f"entry is not balanced (debits minus credits per currency): {unbalanced}")


def create_entry(
    conn: Connection,
    source_event_id: str,
    entry_type: str,
    description: str,
    lines: list[Line],
    business_date: date,
    external_ref: str | None = None,
    reverses_entry_id: int | None = None,
    actor: str = "system",
) -> EntryResult:
    """Write one balanced journal entry atomically and idempotently.

    - Calling it twice with the same `source_event_id` writes ONCE and returns the
      original entry id with created=False. That is what makes webhook retries safe.
    - Must be called inside a transaction (`with engine.begin() as conn`). The header
      and all lines commit together or not at all.
    """
    if entry_type not in VALID_ENTRY_TYPES:
        raise LedgerError(f"unknown entry_type {entry_type!r}")
    validate_lines(lines)

    row = conn.execute(
        text(
            """
            INSERT INTO journal_entries
                (source_event_id, entry_type, external_ref, description, business_date, reverses_entry_id)
            VALUES (:sid, :etype, :ref, :descr, :bdate, :rev)
            ON CONFLICT (source_event_id) DO NOTHING
            RETURNING entry_id
            """
        ),
        {
            "sid": source_event_id,
            "etype": entry_type,
            "ref": external_ref,
            "descr": description,
            "bdate": business_date,
            "rev": reverses_entry_id,
        },
    ).fetchone()

    if row is None:
        # Conflict: this event id was posted before. Return the original, write nothing.
        existing = conn.execute(
            text("SELECT entry_id FROM journal_entries WHERE source_event_id = :sid"),
            {"sid": source_event_id},
        ).scalar_one()
        return EntryResult(int(existing), False)

    entry_id = int(row[0])
    account_ids: dict[str, int] = {}
    params = []
    for ln in lines:
        if ln.account_code not in account_ids:
            account_ids[ln.account_code] = get_account_id(conn, ln.account_code)
        params.append(
            {
                "eid": entry_id,
                "aid": account_ids[ln.account_code],
                "dir": ln.direction,
                "amt": ln.amount_minor,
                "cur": ln.currency,
            }
        )
    conn.execute(
        text(
            """
            INSERT INTO postings (entry_id, account_id, direction, amount_minor, currency)
            VALUES (:eid, :aid, :dir, :amt, :cur)
            """
        ),
        params,
    )
    audit(conn, actor, "post_entry", "journal_entry", entry_id, after={"type": entry_type, "ref": external_ref})
    return EntryResult(entry_id, True)


def reverse_entry(conn: Connection, entry_id: int, reason: str, actor: str = "system") -> EntryResult:
    """Undo an entry by posting its mirror image. Never edits or deletes history.

    Reversing the same entry twice is a no-op (the reversal has a deterministic
    source_event_id), and a reversal itself cannot be reversed.
    """
    original = conn.execute(
        text("SELECT entry_type, business_date, external_ref FROM journal_entries WHERE entry_id = :id"),
        {"id": entry_id},
    ).fetchone()
    if original is None:
        raise LedgerError(f"entry {entry_id} does not exist")
    if original.entry_type == "reversal":
        raise LedgerError("cannot reverse a reversal")

    rows = conn.execute(
        text(
            """
            SELECT a.code, p.direction, p.amount_minor, p.currency
              FROM postings p JOIN accounts a USING (account_id)
             WHERE p.entry_id = :id ORDER BY p.posting_id
            """
        ),
        {"id": entry_id},
    ).fetchall()
    flipped = [
        Line(r.code, "credit" if r.direction == "debit" else "debit", int(r.amount_minor), r.currency)
        for r in rows
    ]
    return create_entry(
        conn,
        source_event_id=f"reversal:{entry_id}",
        entry_type="reversal",
        description=f"Reversal of entry {entry_id}: {reason}",
        lines=flipped,
        business_date=original.business_date,
        external_ref=original.external_ref,
        reverses_entry_id=entry_id,
        actor=actor,
    )
