"""
Generate processor and bank settlement files WITH PLANTED ERRORS.

WHY THIS FILE EXISTS: how do you know a reconciliation engine works? You cannot
check it against real data, because you do not know the "right answer". So we invent
the data ourselves: start from the ledger (the truth), write the files the processor
and bank *should* send, then deliberately corrupt a known list of rows. That list
(the "plan") is the answer key: a correct reconciler must find exactly those breaks,
no more and no fewer. The plan is saved as JSON next to the files.

Corruptions (one transaction is never corrupted in two ways, so each planted
break maps to exactly one expected finding):
  missing_in_processor  row removed from the processor file
  missing_in_ledger     extra "ghost" row in the processor file (we never saw a webhook)
  duplicate             row appears twice in the processor file
  amount_mismatch       gross amount differs from the ledger
  timing_difference     settled_on is 1-3 days after the transaction date
  missing_at_bank       payout absent from the bank file
"""

from __future__ import annotations

import csv
import json
import random
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import Connection, text

from ..ingest.event_handlers import processor_fee

PROCESSOR_COLUMNS = ["txn_id", "type", "gross_minor", "fee_minor", "net_minor", "currency", "txn_date", "settled_on"]
BANK_COLUMNS = ["bank_txn_id", "reference", "amount_minor", "currency", "value_date"]

DEFAULT_COUNTS = {
    "missing_in_processor": 3,
    "duplicate": 3,
    "amount_mismatch": 4,
    "timing_difference": 4,
    "missing_in_ledger": 3,
    "missing_at_bank": 2,
}


def fetch_ledger_rows(conn: Connection, start: date, end: date) -> list[dict]:
    """The ledger's view of the world for [start, end]."""
    rows = conn.execute(
        text(
            """
            SELECT entry_id, entry_type, external_ref, business_date, gross_minor, fee_minor, net_minor
              FROM ledger_transactions
             WHERE business_date BETWEEN :s AND :e
             ORDER BY business_date, external_ref
            """
        ),
        {"s": start, "e": end},
    ).mappings().all()
    return [dict(r) for r in rows]


def build_discrepancy_plan(ledger_rows: list[dict], seed: int = 42, counts: dict | None = None) -> dict:
    """Choose which rows to corrupt and how. Returns the plan (the answer key)."""
    counts = {**DEFAULT_COUNTS, **(counts or {})}
    rng = random.Random(seed)

    txns = sorted((r for r in ledger_rows if r["entry_type"] in ("charge", "refund")), key=lambda r: r["external_ref"])
    payouts = sorted((r for r in ledger_rows if r["entry_type"] == "payout"), key=lambda r: r["external_ref"])
    dates = sorted({r["business_date"] for r in ledger_rows})

    needed = sum(counts[k] for k in ("missing_in_processor", "duplicate", "amount_mismatch", "timing_difference"))
    if needed > len(txns) or counts["missing_at_bank"] > len(payouts):
        raise ValueError("not enough ledger rows to plant that many discrepancies; lower the counts")

    pool = txns[:]
    rng.shuffle(pool)
    planted: list[dict] = []

    def take(n: int) -> list[dict]:
        taken, rest = pool[:n], pool[n:]
        pool[:] = rest
        return taken

    for row in take(counts["missing_in_processor"]):
        planted.append({"break_type": "missing_in_processor", "ref": row["external_ref"],
                        "business_date": row["business_date"].isoformat()})
    for row in take(counts["duplicate"]):
        planted.append({"break_type": "duplicate", "ref": row["external_ref"],
                        "business_date": row["business_date"].isoformat()})
    for row in take(counts["amount_mismatch"]):
        delta = rng.choice([-1, 1, -25, 25, 100, -100, 500])
        if row["gross_minor"] + delta <= 0:
            delta = abs(delta)
        planted.append({"break_type": "amount_mismatch", "ref": row["external_ref"],
                        "business_date": row["business_date"].isoformat(), "delta_minor": delta})
    for row in take(counts["timing_difference"]):
        planted.append({"break_type": "timing_difference", "ref": row["external_ref"],
                        "business_date": row["business_date"].isoformat(), "shift_days": rng.choice([1, 2, 3])})
    for i in range(counts["missing_in_ledger"]):
        gross = rng.randint(1_000, 30_000)
        planted.append({"break_type": "missing_in_ledger", "ref": f"ch_ghost_{i:04d}",
                        "business_date": rng.choice(dates).isoformat(), "gross_minor": gross,
                        "fee_minor": processor_fee(gross)})
    for row in rng.sample(payouts, counts["missing_at_bank"]):
        planted.append({"break_type": "missing_at_bank", "ref": row["external_ref"],
                        "business_date": row["business_date"].isoformat()})

    return {"seed": seed, "counts": counts, "planted": planted}


def apply_plan_to_processor(ledger_rows: list[dict], plan: dict) -> list[dict]:
    """The processor file as it would look WITH the planted errors."""
    by_type: dict[str, dict[str, dict]] = {}
    for item in plan["planted"]:
        by_type.setdefault(item["break_type"], {})[item["ref"]] = item

    rows: list[dict] = []
    for r in ledger_rows:
        if r["entry_type"] not in ("charge", "refund"):
            continue
        ref = r["external_ref"]
        if ref in by_type.get("missing_in_processor", {}):
            continue
        gross, fee = r["gross_minor"], r["fee_minor"]
        settled = r["business_date"]
        if ref in by_type.get("amount_mismatch", {}):
            gross += by_type["amount_mismatch"][ref]["delta_minor"]
        if ref in by_type.get("timing_difference", {}):
            settled = settled + timedelta(days=by_type["timing_difference"][ref]["shift_days"])
        row = {
            "txn_id": ref, "type": r["entry_type"], "gross_minor": gross, "fee_minor": fee,
            "net_minor": gross - fee, "currency": "USD",
            "txn_date": r["business_date"], "settled_on": settled,
        }
        rows.append(row)
        if ref in by_type.get("duplicate", {}):
            rows.append(dict(row))

    for ref, item in by_type.get("missing_in_ledger", {}).items():
        d = date.fromisoformat(item["business_date"])
        rows.append({
            "txn_id": ref, "type": "charge", "gross_minor": item["gross_minor"], "fee_minor": item["fee_minor"],
            "net_minor": item["gross_minor"] - item["fee_minor"], "currency": "USD", "txn_date": d, "settled_on": d,
        })

    rows.sort(key=lambda r: (r["txn_date"], r["txn_id"]))  # stable: duplicates stay adjacent
    return rows


def apply_plan_to_bank(ledger_rows: list[dict], plan: dict) -> list[dict]:
    """The bank file: one deposit per payout, minus the ones we planted as missing."""
    dropped = {p["ref"] for p in plan["planted"] if p["break_type"] == "missing_at_bank"}
    return [
        {"bank_txn_id": f"BK-{r['external_ref']}", "reference": r["external_ref"], "amount_minor": r["gross_minor"],
         "currency": "USD", "value_date": r["business_date"]}
        for r in ledger_rows
        if r["entry_type"] == "payout" and r["external_ref"] not in dropped
    ]


def _write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: (row[c].isoformat() if isinstance(row[c], date) else row[c]) for c in columns})


def write_processor_files(rows: list[dict], out_dir: Path, dates: list[date]) -> list[Path]:
    """One file per day: processor_YYYY-MM-DD.csv (header-only if the day has no rows)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for d in dates:
        path = out_dir / f"processor_{d.isoformat()}.csv"
        _write_csv(path, PROCESSOR_COLUMNS, [r for r in rows if r["txn_date"] == d])
        paths.append(path)
    return paths


def write_bank_files(rows: list[dict], out_dir: Path, dates: list[date]) -> list[Path]:
    """One file per day: bank_YYYY-MM-DD.csv."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for d in dates:
        path = out_dir / f"bank_{d.isoformat()}.csv"
        _write_csv(path, BANK_COLUMNS, [r for r in rows if r["value_date"] == d])
        paths.append(path)
    return paths


def generate_settlement_files(conn: Connection, start: date, end: date, out_dir: Path, seed: int = 42,
                              counts: dict | None = None) -> dict:
    """Full workflow: read ledger -> plan -> write files + plan.json. Returns the plan."""
    ledger_rows = fetch_ledger_rows(conn, start, end)
    plan = build_discrepancy_plan(ledger_rows, seed, counts)
    dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    write_processor_files(apply_plan_to_processor(ledger_rows, plan), out_dir, dates)
    write_bank_files(apply_plan_to_bank(ledger_rows, plan), out_dir, dates)
    plan["start"], plan["end"] = start.isoformat(), end.isoformat()
    Path(out_dir, "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return plan


def load_plan(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
