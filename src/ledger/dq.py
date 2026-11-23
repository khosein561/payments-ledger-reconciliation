"""
Data-quality checks that gate the pipeline.

WHY THIS FILE EXISTS: reconciliation answers "do three sources agree?". These checks
answer an earlier question: "is the ledger itself healthy?" If the books do not sum
to zero, reconciling against them is meaningless, so the pipeline STOPS here instead
of publishing a confident-looking report built on bad data.

The same rules exist as dbt tests (dbt/tests/ and dbt/models/schema.yml) for teams
that run dbt in CI; this Python version is what the DAG and the test-suite execute
directly, so the gate works even without dbt installed.

Severity: 'error' blocks the pipeline; 'warn' is reported but does not block.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import Connection, text

_SIGNED = "CASE direction WHEN 'debit' THEN amount_minor ELSE -amount_minor END"


class DataQualityError(RuntimeError):
    """One or more blocking data-quality checks failed."""


@dataclass
class CheckResult:
    name: str
    passed: bool
    severity: str  # 'error' or 'warn'
    detail: str


def check_ledger_sums_to_zero(conn: Connection) -> CheckResult:
    """Across the whole ledger, total debits must equal total credits (per currency)."""
    rows = conn.execute(
        text(f"SELECT currency, SUM({_SIGNED}) AS diff FROM postings GROUP BY currency HAVING SUM({_SIGNED}) <> 0")
    ).fetchall()
    ok = not rows
    return CheckResult("ledger_sums_to_zero", ok, "error", "balanced" if ok else f"out of balance: {[tuple(r) for r in rows]}")


def check_entries_balanced(conn: Connection) -> CheckResult:
    """Every individual entry must balance, and every entry must have postings."""
    unbalanced = conn.execute(
        text(f"SELECT entry_id FROM postings GROUP BY entry_id, currency HAVING SUM({_SIGNED}) <> 0 LIMIT 10")
    ).fetchall()
    empty = conn.execute(
        text("SELECT e.entry_id FROM journal_entries e LEFT JOIN postings p USING (entry_id) WHERE p.entry_id IS NULL LIMIT 10")
    ).fetchall()
    ok = not unbalanced and not empty
    detail = "all entries balanced" if ok else f"unbalanced={[r[0] for r in unbalanced]} without_postings={[r[0] for r in empty]}"
    return CheckResult("entries_balanced", ok, "error", detail)


def check_webhook_freshness(conn: Connection, max_age_hours: float | None, now: datetime | None = None) -> CheckResult:
    """Warn if no webhook has arrived recently (a silent sender looks identical to a quiet day)."""
    if max_age_hours is None:
        return CheckResult("webhook_freshness", True, "warn", "skipped (no threshold configured)")
    latest = conn.execute(text("SELECT max(received_at) FROM webhook_events")).scalar_one()
    if latest is None:
        return CheckResult("webhook_freshness", False, "warn", "no webhook events have ever been received")
    current = now or datetime.now(timezone.utc)
    age_h = (current - latest).total_seconds() / 3600
    ok = age_h <= max_age_hours
    return CheckResult("webhook_freshness", ok, "warn", f"latest event is {age_h:.1f}h old (limit {max_age_hours}h)")


def check_dead_letters(conn: Connection) -> CheckResult:
    """Warn if parked events are waiting for a human."""
    n = conn.execute(text("SELECT count(*) FROM dead_letter_events WHERE resolved_at IS NULL")).scalar_one()
    return CheckResult("open_dead_letters", n == 0, "warn", f"{n} unresolved dead-letter event(s)")


def run_dq_checks(conn: Connection, max_webhook_age_hours: float | None = None) -> list[CheckResult]:
    return [
        check_ledger_sums_to_zero(conn),
        check_entries_balanced(conn),
        check_webhook_freshness(conn, max_webhook_age_hours),
        check_dead_letters(conn),
    ]


def assert_dq(conn: Connection, max_webhook_age_hours: float | None = None) -> list[CheckResult]:
    """Run all checks; raise DataQualityError if any 'error'-severity check failed."""
    results = run_dq_checks(conn, max_webhook_age_hours)
    failed = [r for r in results if not r.passed and r.severity == "error"]
    if failed:
        raise DataQualityError("; ".join(f"{r.name}: {r.detail}" for r in failed))
    return results
