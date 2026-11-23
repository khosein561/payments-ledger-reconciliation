-- Daily reconciliation scorecard: how much matched, how much broke, and how old the breaks are.
with runs as (
    select run_id, business_date, ledger_count, matched_count, break_count
    from {{ source('ledger', 'recon_runs') }}
    where status = 'completed'
),
breaks as (
    select
        run_id,
        count(*) filter (where break_type = 'missing_in_ledger')     as missing_in_ledger,
        count(*) filter (where break_type = 'missing_in_processor')  as missing_in_processor,
        count(*) filter (where break_type = 'missing_at_bank')       as missing_at_bank,
        count(*) filter (where break_type = 'duplicate')             as duplicates,
        count(*) filter (where break_type = 'amount_mismatch')       as amount_mismatches,
        count(*) filter (where break_type = 'timing_difference')     as timing_differences,
        coalesce(sum(amount_minor), 0)                               as broken_amount_minor
    from {{ source('ledger', 'recon_breaks') }}
    group by run_id
)
select
    r.business_date,
    r.ledger_count,
    r.matched_count,
    r.break_count,
    case when r.ledger_count > 0
         then round(100.0 * r.matched_count / r.ledger_count, 2) end as match_rate_pct,
    coalesce(b.missing_in_ledger, 0)    as missing_in_ledger,
    coalesce(b.missing_in_processor, 0) as missing_in_processor,
    coalesce(b.missing_at_bank, 0)      as missing_at_bank,
    coalesce(b.duplicates, 0)           as duplicates,
    coalesce(b.amount_mismatches, 0)    as amount_mismatches,
    coalesce(b.timing_differences, 0)   as timing_differences,
    coalesce(b.broken_amount_minor, 0)  as broken_amount_minor
from runs r
left join breaks b using (run_id)
