# Architecture

## The two halves

The system has a **real-time half** (events arrive one at a time and become ledger entries) and a **batch half** (once a day, three independent records of the same money are compared).

```text
REAL-TIME                                              BATCH (daily, 06:00)
─────────                                              ────────────────────
Stripe (test mode)                                     processor_YYYY-MM-DD.csv ─┐
   │  POST /webhook  (signed)                          bank_YYYY-MM-DD.csv ──────┤
   ▼                                                                             ▼
FastAPI  ── verify signature ── redact PII             wait_for_files → load_settlement (validate, idempotent)
   │                                                                             │
   ▼                                                                             ▼
webhook_events (PK = event id)  ◄── dedupe             ledger ◄───────────── run_recon ──► recon_runs / matches / breaks
   │                                                    ▲                        │
   ▼                                                    │                        ▼
handle_event → create_entry ──► journal_entries         └── dq_gate ── dbt test ── publish_report ── notify
                                 + postings (append-only, balanced by trigger)
   │ on failure
   ▼
dead_letter_events ──► replay after the cause is fixed
```

## Why it is split this way

| Layer | Responsibility | Why it is separate |
|---|---|---|
| `webhook_app.py` | HTTP only: raw body, signature, status codes | A web framework should not know accounting. Swapping FastAPI for another framework touches one file. |
| `service.py` | Redact → record once → handle → mark; dead-letter policy | One implementation shared by the web endpoint and the simulator, so they cannot drift apart. |
| `event_handlers.py` | Business events → debits and credits | Accounting rules are testable without HTTP. |
| `core/posting.py` | The only door into the ledger | Rules (integers, balance, idempotency, audit) enforced in one place. |
| Postgres constraints/triggers | Balanced entries, append-only, unique event ids, roles | If application code has a bug, the database still refuses to corrupt the books. |
| `recon/match.py` pure functions | Matching logic: lists in, results out | Unit-testable with no database; `run_reconciliation` is a thin wrapper. |
| `pipeline.py` | Steps as plain functions | The scheduler decides *when*; this decides *what*. Same code runs from CLI, tests and Airflow. |

## Key design decisions

1. **Money is integer minor units (cents).** Floating-point cannot represent 0.10 exactly, and errors accumulate across millions of rows. Integers are exact. Python rejects floats before they reach the database, and the database column is `BIGINT`.
2. **The ledger is append-only.** Rows are never edited or deleted. A mistake is fixed by posting a *reversing entry* that mirrors the original. History stays auditable, which is how real accounting systems work. Enforced by triggers, not convention.
3. **Idempotency at two layers.** `webhook_events.event_id` is a primary key (the event is recorded once), and `journal_entries.source_event_id` is unique (the accounting is posted once). Either layer alone would stop duplicates; two layers survive a bug in one.
4. **Balanced entries are enforced at COMMIT.** A *deferred constraint trigger* checks each entry when the transaction commits, so the application may insert the header and its lines in any order but can never commit an unbalanced entry.
5. **Poison events go to a dead-letter table, transient failures get retried.** If the database is down we return `503` and Stripe retries (nothing lost). If an event can never succeed (unsupported currency, bad amount), retrying is pointless, so it is parked for a human and acknowledged with `200`.
6. **PII is redacted before storage.** The ledger needs amounts and ids, not names or emails. Emails are masked; other personal fields are replaced by keyed HMAC tokens that stay stable (so you can still count per customer) but cannot be reversed without the key.
7. **Reconciliation is tested against an answer key.** The generator plants known errors and saves the list. A correct reconciler must find exactly those, with no false alarms.
8. **Replace, don't append, on re-run.** Reconciling a date twice replaces that date's run (inside one transaction), so results never duplicate and a failed re-run leaves the previous run intact.

## Break types

| Type | Meaning | Typical cause |
|---|---|---|
| `missing_in_ledger` | Processor/bank has it, we do not | Missed or failed webhook |
| `missing_in_processor` | We recorded it, processor did not | Wrong event posted; processor file incomplete |
| `missing_at_bank` | Payout recorded, no bank deposit | Bank delay, wrong account |
| `duplicate` | Same transaction appears more than once | File generated twice; double delivery |
| `amount_mismatch` | Both agree it happened, not on the amount | Partial refund, fee change, rounding |
| `timing_difference` | Same amount, different settlement day | Weekend/holiday settlement lag |

## Data model (short form)

- `accounts` – chart of accounts (5 accounts).
- `journal_entries` – one row per business event; unique `source_event_id`; `reverses_entry_id` links a reversal to its original.
- `postings` – the debit/credit lines (`amount_minor BIGINT > 0`).
- `ledger_transactions` (view) – one row per charge / refund / payout as the ledger sees it; reversed entries are excluded.
- `webhook_events`, `dead_letter_events` – intake and parking.
- `processor_settlement`, `bank_settlement` – loaded files; `(source_file, row_num)` unique so loads are idempotent.
- `recon_runs`, `recon_matches`, `recon_breaks` – results; one run per business date.
- `audit_log` – append-only record of who did what.

## Access control

`004_audit_and_roles.sql` creates three group roles: `ledger_writer` (insert only, cannot change or delete history), `recon_job` (reads the ledger, owns reconciliation tables), `analyst_ro` (read-only, no raw event payloads). In production each service gets its own login granted one role. Tests (`test_pipeline_and_dq.py`) prove the denied operations really fail.

## Not covered (see the README's Limitations)

Single currency (USD), simplified fee schedule, no FX, no chargeback lifecycle, no multi-processor routing, single-node deployment.
