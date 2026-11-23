# Failure drills

"Break it on purpose, record what happened." Every row below was actually executed against a real PostgreSQL 16 instance; automated ones are in the test-suite, the manual ones were run against the live server (`python -m ledger serve`). Nothing here is hypothetical.

| # | Drill | Expected | Actual (observed) | Automated? |
|---|---|---|---|---|
| 1 | **Stop Postgres while the API is running**, send a webhook, restart Postgres, let the sender retry | `503` during the outage; the retry is processed **exactly once**; no dead-letter rows | `503 {"detail":"database unavailable, please retry"}` during the outage → after restart the retry returned `200 processed` → a further redelivery returned `200 duplicate`. 1 ledger entry, 0 dead letters. The pooled connection that died with the server was replaced automatically (`pool_pre_ping`). | Manual on live server; in-process equivalent in `test_database_outage_returns_503_so_the_sender_retries_then_recovers` |
| 2 | **Send the same event 5 times** | 1 ledger entry; later deliveries answered `200 duplicate` | statuses `processed, duplicate, duplicate, duplicate, duplicate`; 1 entry | `test_five_deliveries_still_one_entry` |
| 3 | **Forged / tampered / replayed webhook** (wrong secret, edited body, 5-minute-old signature, no header) | `400`; nothing stored | All four rejected with `400`; `webhook_events` and `journal_entries` stayed empty | `test_bad_signature…`, `test_tampered_body…`, `test_stale_timestamp…`, `test_missing_signature…` |
| 4 | **Poison event** (EUR charge, float amount) | Parked in dead letter, acknowledged `200`, redelivery is a duplicate, no ledger entry | As expected. Replay after fixing the cause (accounts missing → re-seeded) posted the entry and marked the dead letter resolved | `test_unsupported_currency…`, `test_float_amount…`, `test_dead_letter_can_be_replayed…` |
| 5 | **Malformed settlement file** (`12.50` instead of cents, `05/01/2026` date, unknown type, wrong net, missing columns) | Rejected whole with a message naming file, line and field; **no rows from it loaded** | Six bad-row variants plus wrong header all rejected; even the valid first row of a bad file was not loaded | `test_settlement_loader.py` |
| 6 | **Corrupt the ledger** (disable the balance trigger, insert a one-sided posting) | Data-quality gate stops the pipeline; dbt tests fail | `DataQualityError: ledger_sums_to_zero…`, `run_daily` refused to continue; `dbt test` → `FAIL 1 assert_ledger_sums_to_zero` and `FAIL 1 assert_every_entry_is_balanced` | `test_dq_gate_blocks_pipeline…`; dbt run manually |
| 7 | **Try to edit history** (`UPDATE`/`DELETE` on postings and entries; `TRUNCATE` as `ledger_writer`) | Refused | `append-only` exception from triggers; `permission denied` for roles | `test_ledger_is_append_only`, `test_ledger_writer_can_insert_but_not_change…` |
| 8 | **Unbalanced entry bypassing Python** (raw SQL) | Rolled back at commit | `journal entry … is not balanced`; zero rows persisted | `test_database_itself_rejects_unbalanced_entry…` |
| 9 | **Re-run reconciliation / re-load a file** | No duplicates | Run count stayed at one per date; break and match counts unchanged; re-load inserted 0 rows | `test_rerunning_reconciliation…`, `test_loading_the_same_file_twice…` |
| 10 | **Boot with a live Stripe key** (`sk_live_…`) | Refuses to start | `ConfigError: Refusing to start: STRIPE_API_KEY must be a TEST key` | Manual |
| 11 | **Mutation check on the test-suite itself**: disable idempotency / remove balance triggers | Tests should fail | Removing the idempotency check → 3 idempotency tests failed. Removing the balance triggers → 3 invariant tests failed. Both restored; 94/94 pass | Manual |

## What was NOT drilled

- Killing the process in the *middle* of a transaction. (Postgres's atomicity guarantees make the outcome "all or nothing"; the outage drill above exercises the closest realistic case.)
- Airflow task retries/backfill: the DAG file was not executed (Airflow was not installed in the build environment).
- Docker Compose (no Docker daemon in the build environment).
- Real Stripe test-mode deliveries via the Stripe CLI. Event shapes follow Stripe's documented envelope, but this has not been run end-to-end against Stripe itself.
