"""
Exception report: what operations opens each morning.

WHY THIS FILE EXISTS: a reconciliation that finds problems nobody reads is useless.
The report lists open breaks, oldest and largest first, with a suggested next action
per break type, in CSV (for spreadsheets) and Markdown (for chat/tickets).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import Connection, text

from .classify import age_bucket

SUGGESTED_ACTION = {
    "missing_in_ledger": "Check webhook delivery logs for the transaction; replay the event if it was missed.",
    "missing_in_processor": "Confirm the transaction exists in the processor dashboard; if not, reverse the ledger entry.",
    "missing_at_bank": "Ask the bank / treasury whether the payout is delayed; escalate if older than 3 days.",
    "duplicate": "Confirm the duplicate is a file artefact, not a double charge; ask the processor to re-issue the file.",
    "amount_mismatch": "Compare ledger and processor amounts; check for partial refunds or fee changes.",
    "timing_difference": "Usually benign; watch for a pattern and confirm it settles within the expected window.",
}

SEVERITY = {
    "missing_in_ledger": "high", "missing_in_processor": "high", "missing_at_bank": "high",
    "duplicate": "medium", "amount_mismatch": "high", "timing_difference": "low",
}


def build_exception_report(conn: Connection, as_of: date, start: date | None = None, end: date | None = None) -> pd.DataFrame:
    """Open breaks from completed runs in [start, end], aged relative to `as_of`."""
    rows = conn.execute(
        text(
            """
            SELECT b.break_type, b.ref, b.amount_minor, b.business_date, b.first_seen_on, b.notes
              FROM recon_breaks b JOIN recon_runs r USING (run_id)
             WHERE r.status = 'completed' AND b.status = 'open'
               AND (CAST(:s AS DATE) IS NULL OR b.business_date >= :s)
               AND (CAST(:e AS DATE) IS NULL OR b.business_date <= :e)
            """
        ),
        {"s": start, "e": end},
    ).mappings().all()

    columns = ["break_type", "ref", "amount_minor", "amount_usd", "business_date", "age_days", "age_bucket",
               "severity", "suggested_action", "notes"]
    if not rows:
        return pd.DataFrame(columns=columns)

    df = pd.DataFrame([dict(r) for r in rows])
    df["age_days"] = df["first_seen_on"].apply(lambda d: max((as_of - d).days, 0))
    df["age_bucket"] = df["age_days"].apply(age_bucket)
    df["amount_usd"] = df["amount_minor"].apply(lambda v: None if pd.isna(v) else round(int(v) / 100, 2))
    df["severity"] = df["break_type"].map(SEVERITY)
    df["suggested_action"] = df["break_type"].map(SUGGESTED_ACTION)
    df = df.sort_values(["age_days", "amount_minor"], ascending=[False, False], kind="stable").reset_index(drop=True)
    return df[columns]


def _md_table(headers: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def write_report(df: pd.DataFrame, out_dir: Path, as_of: date) -> dict:
    """Write exceptions_<date>.csv and exceptions_<date>.md; return the paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"exceptions_{as_of.isoformat()}.csv"
    md_path = out_dir / f"exceptions_{as_of.isoformat()}.md"
    df.to_csv(csv_path, index=False)

    lines = [f"# Reconciliation exceptions as of {as_of.isoformat()}", "", f"Open breaks: **{len(df)}**", ""]
    if len(df):
        by_type = df.groupby("break_type").agg(count=("ref", "count"), total_usd=("amount_usd", "sum")).reset_index()
        lines += ["## By type", "", _md_table(["Type", "Count", "Total USD"],
                  [[r.break_type, r["count"], f"{r.total_usd:,.2f}"] for _, r in by_type.iterrows()]), ""]
        by_age = df.groupby("age_bucket").size().reset_index(name="count")
        lines += ["## By age", "", _md_table(["Age", "Count"], [[r.age_bucket, r["count"]] for _, r in by_age.iterrows()]), ""]
        top = df.head(20)
        lines += ["## Oldest / largest (top 20)", "", _md_table(
            ["Type", "Ref", "USD", "Age (d)", "Severity", "Suggested action"],
            [[r.break_type, r.ref, r.amount_usd, r.age_days, r.severity, r.suggested_action] for _, r in top.iterrows()])]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"csv": csv_path, "markdown": md_path}
