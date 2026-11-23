# Scheduler concept mapping: enterprise batch scheduling → Airflow

Most batch-scheduling ideas transfer directly between tools; only the vocabulary changes. This table translates common concepts from an enterprise scheduler such as Control-M into Airflow, and points at where each one appears in this project. It uses only general, public product concepts: no employer-specific job names, schedules or data.

| Enterprise scheduler concept | Airflow equivalent | In this project |
|---|---|---|
| **Job** | **Task** (an operator instance) | `load_settlement`, `run_recon`, `dq_gate` … in `dags/daily_reconciliation_dag.py` |
| **Folder / job group** | **DAG** | `daily_reconciliation` |
| **In/Out conditions** (job B waits for job A's condition) | **Dependencies** (`a >> b`) | `[wait_processor, wait_bank] >> load >> recon >> dq >> dbt_test >> publish_report >> notify` |
| **File-watcher job** | **Sensor** (`FileSensor`) | `wait_for_processor_file`, `wait_for_bank_file`, with `mode="reschedule"` so a waiting sensor does not hold a worker slot |
| **Calendar / run schedule** | **Schedule / timetable** (`cron`) | `schedule="0 6 * * *"` |
| **Order date (ODATE)** — the business date a run belongs to | **Logical date** (`ds`) | Tasks receive `ds` and process *that* date, not "today" — the reason a late rerun reconciles the right day |
| **Rerun / restart from a failed step** | **Clear a task**, or retries | Tasks are idempotent, so re-running one is safe |
| **Automatic rerun on failure** | `retries`, `retry_delay`, exponential backoff | `retries=2`, 5-minute delay, backoff on |
| **Ordering a missed day / catch-up** | **Backfill** / `catchup=True` | `airflow dags backfill …` re-runs past dates; idempotent steps make it safe |
| **SLA / must-complete-by** | DAG run timeout; SLA / deadline alerts (version-dependent) | `dagrun_timeout=2h` after a 06:00 start ≈ 08:00 deadline |
| **Shout / notification on failure** | `on_failure_callback` | `alert_failure` → `pipeline.notify` |
| **Quantitative resource** (limit concurrent jobs) | **Pools** / `max_active_runs` | `max_active_runs=1` |
| **Hold / free a job** | **Pause DAG** / mark task | Pause the DAG in the UI during an incident |
| **Cyclic (every N minutes) job** | Short-interval schedule, or a sensor | A sensor with `poke_interval=300` |
| **Run-as user / connection profile** | **Connections** and secrets backends | Credentials come from environment variables, never the DAG file |

## What is genuinely different

1. **Pipelines are code.** A DAG is a Python file in git: reviewable, testable, diffable. Many enterprise schedulers store definitions in a database or GUI, so change control is a separate process.
2. **Data-aware design is on you.** The scheduler runs tasks in order; it does not know whether a task *double-loaded* data. That is why every step here is idempotent (unique keys + `ON CONFLICT DO NOTHING`, replace-on-rerun). A scheduler "rerun" button is only safe if the job itself tolerates reruns. Experienced batch operators already think this way; this project writes it down.
3. **Templating.** `{{ ds }}` in `FileSensor.filepath` is rendered at run time, the Airflow counterpart of date variables in job definitions.
4. **Version drift.** Airflow 3 changed imports (`airflow.sdk`, standard provider) and replaced SLAs with deadline alerts. The DAG file therefore tries Airflow 3 imports first, then falls back to 2.x.

*The DAG itself has not been executed in the environment where this project was built; the step functions it calls are covered by the test suite.*
