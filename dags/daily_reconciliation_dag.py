"""
Airflow DAG: nightly payments reconciliation.

WHY THIS FILE EXISTS: the reconciliation logic is plain Python (src/ledger/pipeline.py);
this file only says WHEN it runs and in WHAT ORDER, with retries, a deadline and
backfill. Keeping orchestration thin is deliberate: the same steps run from the CLI,
the tests, or any other scheduler.

    wait_for_files -> load_settlement -> run_recon -> dq_gate -> dbt_test -> publish_report -> notify

STATUS: written against the Airflow 2.x and 3.x APIs but NOT executed in the sandbox
where this project was built (Airflow was not installed there). The step functions it
calls ARE covered by the test-suite (tests/test_pipeline_and_dq.py). Treat the first
`airflow dags test daily_reconciliation 2026-01-05` as your integration test.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

try:  # Airflow 3
    from airflow.sdk import DAG
    from airflow.providers.standard.operators.bash import BashOperator
    from airflow.providers.standard.operators.python import PythonOperator
    from airflow.providers.standard.sensors.filesystem import FileSensor
except ImportError:  # Airflow 2.x
    from airflow import DAG
    from airflow.operators.bash import BashOperator
    from airflow.operators.python import PythonOperator
    from airflow.sensors.filesystem import FileSensor

SETTLEMENT_DIR = Path(os.getenv("SETTLEMENT_DIR", "/opt/airflow/data/settlement"))
REPORT_DIR = Path(os.getenv("REPORT_DIR", "/opt/airflow/data/reports"))
DBT_DIR = os.getenv("DBT_PROJECT_DIR", "/opt/airflow/dbt")


def _date(ds: str):
    from datetime import date

    return date.fromisoformat(ds)


def _engine():
    from ledger.db import get_engine

    return get_engine()


def alert_failure(context) -> None:
    """Called by Airflow when a task finally fails (after its retries)."""
    from ledger.pipeline import notify

    ti = context.get("task_instance")
    notify(f"Reconciliation DAG failed: task={getattr(ti, 'task_id', '?')} date={context.get('ds')}")


def task_load(ds: str, **_):
    from ledger.pipeline import load_settlement

    return load_settlement(_engine(), _date(ds), SETTLEMENT_DIR)


def task_recon(ds: str, **_):
    from ledger.pipeline import reconcile_date

    return reconcile_date(_engine(), _date(ds))


def task_dq(**_):
    from ledger.pipeline import dq_gate

    return [r.__dict__ for r in dq_gate(_engine())]


def task_publish(ds: str, **_):
    from ledger.pipeline import publish

    df, paths = publish(_engine(), _date(ds), REPORT_DIR)
    return {"open_breaks": len(df), **{k: str(v) for k, v in paths.items()}}


def task_notify(ds: str, ti=None, **_):
    from ledger.pipeline import notify

    summary = ti.xcom_pull(task_ids="publish_report") or {}
    high = summary.get("open_breaks", 0)
    if high:
        notify(f"{ds}: {high} open reconciliation break(s). Report: {summary.get('markdown')}")


default_args = {
    "owner": "data-eng",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "retry_exponential_backoff": True,   # 5 min, then ~10 min
    "on_failure_callback": alert_failure,
}

with DAG(
    dag_id="daily_reconciliation",
    description="Load settlement files, reconcile ledger vs processor vs bank, publish exceptions",
    schedule="0 6 * * *",                       # 06:00 every day
    start_date=datetime(2026, 1, 1),
    catchup=True,                               # lets you backfill missed days
    max_active_runs=1,                          # one date at a time: tasks are idempotent but ordering is easier to reason about
    dagrun_timeout=timedelta(hours=2),          # deadline: a run still going at ~08:00 is treated as failed and alerted
    default_args=default_args,
    tags=["finance", "reconciliation"],
) as dag:

    wait_processor = FileSensor(
        task_id="wait_for_processor_file",
        filepath=str(SETTLEMENT_DIR / "processor_{{ ds }}.csv"),
        poke_interval=300, timeout=60 * 60, mode="reschedule",   # 'reschedule' frees the worker between checks
    )
    wait_bank = FileSensor(
        task_id="wait_for_bank_file",
        filepath=str(SETTLEMENT_DIR / "bank_{{ ds }}.csv"),
        poke_interval=300, timeout=60 * 60, mode="reschedule",
    )
    load = PythonOperator(task_id="load_settlement", python_callable=task_load)
    recon = PythonOperator(task_id="run_recon", python_callable=task_recon)
    dq = PythonOperator(task_id="dq_gate", python_callable=task_dq)
    dbt_test = BashOperator(
        task_id="dbt_test",
        bash_command=f"cd {DBT_DIR} && dbt test --profiles-dir . --fail-fast",
    )
    publish_report = PythonOperator(task_id="publish_report", python_callable=task_publish)
    notify_task = PythonOperator(task_id="notify", python_callable=task_notify)

    [wait_processor, wait_bank] >> load >> recon >> dq >> dbt_test >> publish_report >> notify_task
