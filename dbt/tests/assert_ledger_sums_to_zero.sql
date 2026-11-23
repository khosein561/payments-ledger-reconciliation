-- A dbt "singular test": it PASSES when this query returns zero rows.
-- Returns one row per currency whose total debits and credits differ.
select currency, sum(signed_amount_minor) as out_of_balance_minor
from {{ ref('stg_postings') }}
group by currency
having sum(signed_amount_minor) <> 0
