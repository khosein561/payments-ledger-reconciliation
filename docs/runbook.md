# Runbook

Written so someone on call who has never seen this system can act without asking the author. All SQL is read-only unless stated.

## Quick health check

```bash
curl -s localhost:8000/healthz                       # {"status":"ok"} or 503
python -m ledger migrate                             # safe to run any time; reports "already up to date"
```
```sql
-- Is the ledger healthy? Both must return zero rows.
SELECT currency, SUM(CASE direction WHEN 'debit' THEN amount_minor ELSE -amount_minor END)
  FROM postings GROUP BY currency HAVING SUM(CASE direction WHEN 'debit' THEN amount_minor ELSE -amount_minor END) <> 0;
SELECT e.entry_id FROM journal_entries e LEFT JOIN postings p USING (entry_id) WHERE p.entry_id IS NULL;
```

## Symptom table

| Symptom | Likely cause | Check | Fix | Escalate when |
|---|---|---|---|---|
| Webhook endpoint returns **503** | Postgres down or unreachable | `/healthz`; `docker compose ps postgres`; `docker compose logs postgres` | Restore the database. **Do nothing else**: Stripe retries 503s automatically, and replays are safe (idempotent). | Down > 30 min: confirm Stripe's retry window has not been exhausted; if it has, replay missed events from the Stripe dashboard. |
| Webhook endpoint returns **400** at a high rate | Wrong `STRIPE_WEBHOOK_SECRET` after a rotation, or someone probing the URL | App logs: `rejected webhook: signature does not match` vs `timestamp outside the tolerance window` | Wrong secret → update `.env`, restart `api`. Tolerance errors → check the server clock (NTP). | Many 400s with *valid-looking* traffic from unfamiliar IPs. |
| **Dead-letter queue growing** | New event type/shape; unsupported currency; accounts not seeded | `SELECT id, event_id, error, failed_at FROM dead_letter_events WHERE resolved_at IS NULL ORDER BY failed_at;` | Fix the cause (code or data), then `python -m ledger replay <id>` for each. Replay is idempotent. | Same error repeats for > 10 events, or any error mentioning "not balanced". |
| **Recon DAG missed its deadline** | Settlement file late; DB slow; earlier task retrying | Airflow UI: which task is running/failed? `wait_for_*_file` sensors still poking = file not delivered | Late file → contact the processor/bank for a re-send, then clear the failed task. Slow DB → check locks (`pg_stat_activity`). | 2 consecutive days. |
| **`ledger_sums_to_zero` / `entries_balanced` fails** | Corruption: constraint triggers disabled, direct SQL bypass, or a migration bug | Run the health-check SQL above to find the entry | **Stop the pipeline.** Do not edit rows (the ledger forbids it). Identify the entry; post a reversing entry with `reverse_entry`. Find out how the trigger was bypassed. | Immediately. This should be impossible in normal operation. |
| **Many `missing_in_ledger` breaks** | Webhooks not arriving or failing | Compare `webhook_events` count per day to processor volume; check dead letters; check Stripe dashboard → Webhooks → failed deliveries | Fix delivery, replay missed events, rerun recon for the date | A single day > 1% of volume. |
| **Many `amount_mismatch` breaks** | Fee schedule changed, partial refunds, currency mismatch | Read the `notes` column: it shows both sides' numbers | Update `processor_fee` if the schedule changed; otherwise treat each as an investigation | Systematic (same delta repeated). |
| **`timing_difference` pattern** | Weekend/holiday settlement lag | Group by weekday: `SELECT extract(dow FROM business_date), count(*) FROM recon_breaks WHERE break_type='timing_difference' GROUP BY 1;` | Usually none; consider widening `tolerance_days` | Differences > 3 days (these are not auto-matched). |
| **`missing_at_bank` older than 3 days** | Bank delay or wrong destination account | Exception report age bucket; treasury confirmation | Ask treasury/bank; escalate | > 3 days. |
| **Settlement file rejected on load** | Malformed file from the sender | Error message names the file, line, and field | Ask the sender for a corrected file; nothing from the bad file was loaded | Repeated from the same sender. |

## Common operations

```bash
# Re-run reconciliation for a date (replaces that date's results, never duplicates)
python -m ledger recon --date 2026-01-05

# Replay a parked event after fixing the cause
python -m ledger replay 12

# Backfill a week (Airflow): airflow dags backfill daily_reconciliation -s 2026-01-01 -e 2026-01-07
```

```sql
-- Open breaks, worst first
SELECT break_type, ref, amount_minor, business_date, notes
  FROM recon_breaks WHERE status = 'open' ORDER BY business_date, amount_minor DESC NULLS LAST;

-- Mark one as understood (recon_breaks is allowed to change; the ledger is not)
UPDATE recon_breaks SET status = 'investigating', notes = notes || ' | ticket OPS-123' WHERE break_id = 42;
```

## What never to do

- Never `UPDATE` or `DELETE` ledger rows. The triggers will refuse; if you disable the triggers to force it, you have destroyed the audit trail. Post a reversing entry instead.
- Never use a live Stripe key. The app refuses to start with one.
- Never put real customer data into this system's dev or test environments.
- Never commit `.env`.

## Dev-only

`python -m ledger reset --i-understand-this-deletes-everything` truncates every table. It exists for demos. The production role `ledger_writer` has no `TRUNCATE` privilege, and the test-suite proves it.
