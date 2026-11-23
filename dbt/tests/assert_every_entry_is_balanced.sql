-- Passes when it returns zero rows: every journal entry balances within each currency.
select entry_id, currency, sum(signed_amount_minor) as out_of_balance_minor
from {{ ref('stg_postings') }}
group by entry_id, currency
having sum(signed_amount_minor) <> 0
