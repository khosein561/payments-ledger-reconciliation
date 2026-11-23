-- 001_ledger_core.sql
-- Core double-entry ledger.
--
-- Design rules enforced IN THE DATABASE (not just in Python), so no bug in the
-- application can corrupt the books:
--   1. Money is BIGINT minor units (cents). Never FLOAT/NUMERIC-with-rounding.
--   2. Every journal entry must balance (debits = credits, per currency) at COMMIT.
--   3. The ledger is append-only. Mistakes are fixed with a reversing entry.
--   4. source_event_id is UNIQUE, so replaying the same business event can never
--      post twice (idempotency).

CREATE TABLE accounts (
    account_id  SERIAL PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL CHECK (type IN ('asset', 'liability', 'revenue', 'expense', 'equity')),
    currency    CHAR(3) NOT NULL DEFAULT 'USD'
);

CREATE TABLE journal_entries (
    entry_id          BIGSERIAL PRIMARY KEY,
    source_event_id   TEXT NOT NULL UNIQUE,            -- idempotency key (e.g. the Stripe evt_... id)
    entry_type        TEXT NOT NULL CHECK (entry_type IN ('charge', 'refund', 'payout', 'reversal', 'adjustment')),
    external_ref      TEXT,                            -- charge / refund / payout id used by reconciliation
    description       TEXT NOT NULL,
    business_date     DATE NOT NULL,                   -- the accounting date, not the insert time
    reverses_entry_id BIGINT REFERENCES journal_entries (entry_id),
    posted_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_journal_entries_external_ref ON journal_entries (external_ref);
CREATE INDEX idx_journal_entries_business_date ON journal_entries (business_date);
CREATE UNIQUE INDEX uq_journal_entries_one_reversal
    ON journal_entries (reverses_entry_id) WHERE reverses_entry_id IS NOT NULL;

CREATE TABLE postings (
    posting_id    BIGSERIAL PRIMARY KEY,
    entry_id      BIGINT NOT NULL REFERENCES journal_entries (entry_id),
    account_id    INT    NOT NULL REFERENCES accounts (account_id),
    direction     TEXT   NOT NULL CHECK (direction IN ('debit', 'credit')),
    amount_minor  BIGINT NOT NULL CHECK (amount_minor > 0),
    currency      CHAR(3) NOT NULL DEFAULT 'USD'
);

CREATE INDEX idx_postings_entry_id ON postings (entry_id);
CREATE INDEX idx_postings_account_id ON postings (account_id);

-- ---------------------------------------------------------------------------
-- Balanced-entry check, evaluated when the transaction COMMITS (DEFERRED), so
-- the application can insert the entry header and its lines in any order.
-- ---------------------------------------------------------------------------
CREATE FUNCTION assert_entry_balanced() RETURNS trigger AS $$
DECLARE
    bad RECORD;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM postings WHERE entry_id = NEW.entry_id) THEN
        RAISE EXCEPTION 'journal entry % has no postings', NEW.entry_id;
    END IF;

    SELECT currency,
           SUM(CASE direction WHEN 'debit' THEN amount_minor ELSE -amount_minor END) AS diff
      INTO bad
      FROM postings
     WHERE entry_id = NEW.entry_id
     GROUP BY currency
    HAVING SUM(CASE direction WHEN 'debit' THEN amount_minor ELSE -amount_minor END) <> 0
     LIMIT 1;

    IF FOUND THEN
        RAISE EXCEPTION 'journal entry % is not balanced (currency %, debits minus credits = %)',
            NEW.entry_id, bad.currency, bad.diff;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER trg_postings_balanced
    AFTER INSERT ON postings
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_entry_balanced();

CREATE CONSTRAINT TRIGGER trg_entries_balanced
    AFTER INSERT ON journal_entries
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_entry_balanced();

-- ---------------------------------------------------------------------------
-- Append-only: block UPDATE and DELETE on the books.
-- ---------------------------------------------------------------------------
CREATE FUNCTION forbid_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% on % is not allowed: the ledger is append-only (post a reversing entry instead)',
        TG_OP, TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_journal_entries_append_only
    BEFORE UPDATE OR DELETE ON journal_entries
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TRIGGER trg_postings_append_only
    BEFORE UPDATE OR DELETE ON postings
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
