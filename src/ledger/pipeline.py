"""
The daily reconciliation pipeline as plain, individually callable steps.

WHY THIS FILE EXISTS: orchestrators (Airflow, Control-M, cron) should only decide
WHEN and IN WHAT ORDER things run. The actual work lives here as ordinary Python
functions, so it can be tested, run by hand (`python -m ledger recon --date ...`),
and called by the Airflow DAG without any scheduler-specific code mixed in.

Every step is idempotent: running it twice for the same date leaves the database in
the same state. That is what makes retries and backfills safe.

    load_settlement -> reconcile_date -> dq_gate -> publish -> notify
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import Engine

from .dq import CheckResult, assert_dq
from .recon.load import load_bank_file, load_processor_file
from .recon.match import run_reconciliation
from .recon.report import build_exception_report, write_report

log = logging.getLogger("ledger.pipeline")


def settlement_paths(settlement_dir: Path, d: date) -> tuple[Path, Path]:
    return Path(settlement_dir) / f"processor_{d.isoformat()}.csv", Path(settlement_dir) / f"bank_{d.isoformat()}.csv"


def files_ready(settlement_dir: Path, d: date) -> bool:
    return all(p.exists() for p in settlement_paths(settlement_dir, d))


def load_settlement(engine: Engine, d: date, settlement_dir: Path) -> dict:
    """Step 1: validate and load the day's two files."""
    proc_path, bank_path = settlement_paths(settlement_dir, d)
    for p in (proc_path, bank_path):
        if not p.exists():
            raise FileNotFoundError(f"settlement file not found: {p}")
    with engine.begin() as conn:
        new_proc = load_processor_file(conn, proc_path)
        new_bank = load_bank_file(conn, bank_path)
    return {"processor_rows_loaded": new_proc, "bank_rows_loaded": new_bank}


def reconcile_date(engine: Engine, d: date, tolerance_days: int = 3) -> int:
    """Step 2: match ledger vs processor vs bank for the date."""
    with engine.begin() as conn:
        return run_reconciliation(conn, d, tolerance_days)


def dq_gate(engine: Engine, max_webhook_age_hours: float | None = None) -> list[CheckResult]:
    """Step 3: stop the pipeline if the ledger itself is unhealthy."""
    with engine.begin() as conn:
        return assert_dq(conn, max_webhook_age_hours)


def publish(engine: Engine, d: date, report_dir: Path) -> tuple[pd.DataFrame, dict]:
    """Step 4: write the exception report."""
    with engine.begin() as conn:
        df = build_exception_report(conn, as_of=d)
    return df, write_report(df, report_dir, d)


def notify(message: str) -> None:
    """Step 5: tell a human. Logs always; also POSTs to ALERT_WEBHOOK_URL if configured."""
    log.warning("ALERT: %s", message)
    url = os.getenv("ALERT_WEBHOOK_URL")
    if not url:
        return
    try:
        req = urllib.request.Request(
            url, data=json.dumps({"text": message}).encode("utf-8"), headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=10)  # noqa: S310 - URL comes from our own configuration
    except Exception:  # noqa: BLE001 - a failed alert must never fail the pipeline
        log.exception("could not send alert")


def run_daily(
    engine: Engine, d: date, settlement_dir: Path, report_dir: Path,
    tolerance_days: int = 3, high_severity_alert_threshold: int = 1,
) -> dict:
    """Run every step for one business date and return a summary."""
    loaded = load_settlement(engine, d, settlement_dir)
    run_id = reconcile_date(engine, d, tolerance_days)
    dq_gate(engine)
    df, paths = publish(engine, d, report_dir)

    high = int((df["severity"] == "high").sum()) if len(df) else 0
    if high >= high_severity_alert_threshold:
        notify(f"{d.isoformat()}: {high} high-severity reconciliation break(s). Report: {paths['markdown']}")
    return {"date": d.isoformat(), "run_id": run_id, "open_breaks": len(df), "high_severity": high, **loaded, **paths}
