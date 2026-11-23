"""
Chart of accounts.

WHY THIS FILE EXISTS: double-entry bookkeeping needs a fixed list of accounts to
post against. Keeping the list in one place (and seeding it idempotently) means every
environment starts with the same books.

How the accounts are used (all amounts positive; direction says which side):

  charge  $100.00, fee $3.20:
      DEBIT  PROCESSOR_CLEARING  100.00   (the processor now owes us this money)
      CREDIT REVENUE             100.00
      DEBIT  FEES_EXPENSE          3.20
      CREDIT PROCESSOR_CLEARING    3.20   (they keep their fee out of what they owe us)
  refund  $20.00:
      DEBIT  REFUNDS              20.00
      CREDIT PROCESSOR_CLEARING   20.00
  payout  $76.80 (the processor sends money to our bank):
      DEBIT  CASH                 76.80
      CREDIT PROCESSOR_CLEARING   76.80

If every charge/refund is paid out, PROCESSOR_CLEARING ends at zero. A non-zero
clearing balance is "money the processor still holds for us", which is exactly what
reconciliation checks.
"""

from __future__ import annotations

from sqlalchemy import Connection, text

CHART_OF_ACCOUNTS = [
    ("CASH", "Cash at bank", "asset"),
    ("PROCESSOR_CLEARING", "Funds held by the payment processor", "asset"),
    ("REVENUE", "Sales revenue", "revenue"),
    ("REFUNDS", "Refunds issued to customers", "expense"),
    ("FEES_EXPENSE", "Payment processing fees", "expense"),
]


def seed_accounts(conn: Connection) -> int:
    """Insert the standard accounts if missing. Safe to run any number of times."""
    inserted = 0
    for code, name, acct_type in CHART_OF_ACCOUNTS:
        result = conn.execute(
            text(
                """
                INSERT INTO accounts (code, name, type) VALUES (:code, :name, :type)
                ON CONFLICT (code) DO NOTHING
                """
            ),
            {"code": code, "name": name, "type": acct_type},
        )
        inserted += result.rowcount or 0
    return inserted


def get_account_id(conn: Connection, code: str) -> int:
    """Look up an account id by its code. Raises KeyError for an unknown code."""
    row = conn.execute(text("SELECT account_id FROM accounts WHERE code = :code"), {"code": code}).fetchone()
    if row is None:
        raise KeyError(f"unknown account code: {code!r} (did you run seed_accounts?)")
    return int(row[0])
