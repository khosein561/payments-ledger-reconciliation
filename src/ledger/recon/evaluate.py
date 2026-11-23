"""
Score the reconciler against the planted answer key.

WHY THIS FILE EXISTS: "it seems to work" is not evidence. Because we planted the
errors ourselves, we know exactly which breaks exist. This compares what the
reconciler FOUND to what we PLANTED and reports, per break type:

    planted         how many we hid
    found           how many the reconciler reported
    true_positive   reported AND planted
    false_positive  reported but NOT planted   (a false alarm)
    missed          planted but NOT reported   (a miss)

These numbers feed the Results table in the README.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
from sqlalchemy import Connection, text

from .classify import BreakType


def found_breaks(conn: Connection, start: date, end: date) -> set[tuple[str, str]]:
    rows = conn.execute(
        text(
            """
            SELECT b.break_type, b.ref
              FROM recon_breaks b JOIN recon_runs r USING (run_id)
             WHERE r.status = 'completed' AND r.business_date BETWEEN :s AND :e
            """
        ),
        {"s": start, "e": end},
    ).fetchall()
    return {(r.break_type, r.ref) for r in rows}


def planted_breaks(plan: dict) -> set[tuple[str, str]]:
    return {(p["break_type"], p["ref"]) for p in plan["planted"]}


def score_against_plan(conn: Connection, plan: dict, start: date, end: date) -> pd.DataFrame:
    planted = planted_breaks(plan)
    found = found_breaks(conn, start, end)
    rows = []
    for bt in BreakType:
        p = {x for x in planted if x[0] == bt.value}
        f = {x for x in found if x[0] == bt.value}
        rows.append({
            "break_type": bt.value, "planted": len(p), "found": len(f),
            "true_positive": len(p & f), "false_positive": len(f - p), "missed": len(p - f),
        })
    df = pd.DataFrame(rows)
    totals = {"break_type": "TOTAL", **{c: int(df[c].sum()) for c in df.columns if c != "break_type"}}
    return pd.concat([df, pd.DataFrame([totals])], ignore_index=True)


def to_markdown(df: pd.DataFrame) -> str:
    cols = ["break_type", "planted", "found", "false_positive", "missed"]
    header = "| Break type | Planted | Found | False positives | Missed |\n|---|---|---|---|---|"
    body = "\n".join(
        f"| {r.break_type} | {r.planted} | {r.found} | {r.false_positive} | {r.missed} |" for r in df[cols].itertuples()
    )
    return header + "\n" + body
