-- One row per posting with the sign applied: debits positive, credits negative.
-- Summing signed_amount_minor over any complete set of postings must give 0.
select
    p.posting_id,
    p.entry_id,
    e.entry_type,
    e.business_date,
    a.code as account_code,
    a.type as account_type,
    p.direction,
    p.currency,
    p.amount_minor,
    case p.direction when 'debit' then p.amount_minor else -p.amount_minor end as signed_amount_minor
from {{ source('ledger', 'postings') }} p
join {{ source('ledger', 'journal_entries') }} e on e.entry_id = p.entry_id
join {{ source('ledger', 'accounts') }} a on a.account_id = p.account_id
