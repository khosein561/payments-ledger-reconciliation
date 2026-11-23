-- 003_reconciliation.sql
-- Settlement files (what the processor and the bank say happened), the
-- reconciliation results, and a view of what OUR ledger says happened.

-- Loaded from the processor's settlement CSV. (source_file, row_num) makes loading
-- idempotent: loading the same file twice inserts nothing new.
CREATE TABLE processor_settlement (
    id            BIGSERIAL PRIMARY KEY,
    source_file   TEXT NOT NULL,
    row_num       INT  NOT NULL,
    txn_id        TEXT NOT NULL,
    type          TEXT NOT NULL CHECK (type IN ('charge', 'refund')),
    gross_minor   BIGINT NOT NULL CHECK (gross_minor >= 0),
    fee_minor     BIGINT NOT NULL CHECK (fee_minor >= 0),
    net_minor     BIGINT NOT NULL,
    currency      CHAR(3) NOT NULL,
    txn_date      DATE NOT NULL,      -- the day the transaction happened
    settled_on    DATE NOT NULL,      -- the day the processor says it settled
    UNIQUE (source_file, row_num)
);
CREATE INDEX idx_processor_settlement_txn_date ON processor_settlement (txn_date);
CREATE INDEX idx_processor_settlement_txn_id ON processor_settlement (txn_id);

CREATE TABLE bank_settlement (
    id             BIGSERIAL PRIMARY KEY,
    source_file    TEXT NOT NULL,
    row_num        INT  NOT NULL,
    bank_txn_id    TEXT NOT NULL,
    reference      TEXT NOT NULL,     -- the payout id the bank quotes
    amount_minor   BIGINT NOT NULL,
    currency       CHAR(3) NOT NULL,
    value_date     DATE NOT NULL,
    UNIQUE (source_file, row_num)
);
CREATE INDEX idx_bank_settlement_value_date ON bank_settlement (value_date);

CREATE TABLE recon_runs (
    run_id          BIGSERIAL PRIMARY KEY,
    business_date   DATE NOT NULL UNIQUE,   -- one current run per date; re-running replaces it
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'completed', 'failed')),
    ledger_count    INT,
    processor_count INT,
    bank_count      INT,
    matched_count   INT,
    break_count     INT
);

CREATE TABLE recon_matches (
    match_id           BIGSERIAL PRIMARY KEY,
    run_id             BIGINT NOT NULL REFERENCES recon_runs (run_id) ON DELETE CASCADE,
    ledger_entry_id    BIGINT,
    processor_txn_id   TEXT,
    bank_txn_id        TEXT,
    match_pass         TEXT NOT NULL CHECK (match_pass IN ('exact', 'tolerant'))
);
CREATE INDEX idx_recon_matches_run ON recon_matches (run_id);

CREATE TABLE recon_breaks (
    break_id        BIGSERIAL PRIMARY KEY,
    run_id          BIGINT NOT NULL REFERENCES recon_runs (run_id) ON DELETE CASCADE,
    break_type      TEXT NOT NULL CHECK (break_type IN (
                        'missing_in_ledger', 'missing_in_processor', 'missing_at_bank',
                        'duplicate', 'amount_mismatch', 'timing_difference')),
    ref             TEXT NOT NULL,          -- the txn / payout id involved
    amount_minor    BIGINT,
    business_date   DATE NOT NULL,
    first_seen_on   DATE NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'investigating', 'resolved')),
    notes           TEXT
);
CREATE INDEX idx_recon_breaks_run ON recon_breaks (run_id);
CREATE INDEX idx_recon_breaks_open ON recon_breaks (business_date) WHERE status = 'open';

-- ---------------------------------------------------------------------------
-- What OUR ledger says happened, one row per charge / refund / payout.
-- Reversed entries (and the reversals themselves) are excluded: the ledger is
-- saying "that never happened", so reconciliation should see it that way.
-- ---------------------------------------------------------------------------
CREATE VIEW ledger_transactions AS
WITH lines AS (
    SELECT e.entry_id,
           e.entry_type,
           e.external_ref,
           e.business_date,
           COALESCE(SUM(p.amount_minor) FILTER (
               WHERE (e.entry_type = 'charge' AND a.code = 'REVENUE' AND p.direction = 'credit')
                  OR (e.entry_type = 'refund' AND a.code = 'REFUNDS' AND p.direction = 'debit')
                  OR (e.entry_type = 'payout' AND a.code = 'CASH'    AND p.direction = 'debit')
           ), 0) AS gross_minor,
           COALESCE(SUM(p.amount_minor) FILTER (
               WHERE a.code = 'FEES_EXPENSE' AND p.direction = 'debit'
           ), 0) AS fee_minor
      FROM journal_entries e
      JOIN postings p USING (entry_id)
      JOIN accounts a USING (account_id)
     WHERE e.entry_type IN ('charge', 'refund', 'payout')
       AND NOT EXISTS (SELECT 1 FROM journal_entries r WHERE r.reverses_entry_id = e.entry_id)
     GROUP BY e.entry_id, e.entry_type, e.external_ref, e.business_date
)
SELECT entry_id, entry_type, external_ref, business_date,
       gross_minor, fee_minor, gross_minor - fee_minor AS net_minor
  FROM lines;
