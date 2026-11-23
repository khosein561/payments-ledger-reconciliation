-- 004_audit_and_roles.sql
-- Audit trail and least-privilege database roles.
--
-- These are GROUP roles (NOLOGIN). In a real deployment you create one LOGIN
-- user per service and GRANT it the matching group role, e.g.
--     CREATE ROLE webhook_api LOGIN PASSWORD '...';  GRANT ledger_writer TO webhook_api;
-- so a leaked credential for one service can only do that service's job.

CREATE TABLE audit_log (
    id           BIGSERIAL PRIMARY KEY,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    object_type  TEXT NOT NULL,
    object_id    TEXT,
    before       JSONB,
    after        JSONB,
    at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE FUNCTION forbid_audit_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_audit_append_only
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION forbid_audit_mutation();

-- A view analysts can query: no raw payloads, even the redacted ones.
CREATE VIEW analyst_webhook_events AS
SELECT event_id, event_type, status, received_at, processed_at
  FROM webhook_events;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ledger_writer') THEN
        CREATE ROLE ledger_writer NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'recon_job') THEN
        CREATE ROLE recon_job NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'analyst_ro') THEN
        CREATE ROLE analyst_ro NOLOGIN;
    END IF;
END
$$;

-- ledger_writer: the webhook service. Can add to the books, never change them.
GRANT SELECT ON accounts TO ledger_writer;
GRANT SELECT, INSERT ON journal_entries, postings TO ledger_writer;
GRANT SELECT, INSERT, UPDATE ON webhook_events, dead_letter_events TO ledger_writer;
GRANT INSERT ON audit_log TO ledger_writer;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ledger_writer;

-- recon_job: reads the books, owns the reconciliation tables.
GRANT SELECT ON accounts, journal_entries, postings, ledger_transactions, webhook_events, dead_letter_events TO recon_job;
GRANT SELECT, INSERT, UPDATE, DELETE ON processor_settlement, bank_settlement,
      recon_runs, recon_matches, recon_breaks TO recon_job;
GRANT INSERT ON audit_log TO recon_job;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO recon_job;

-- analyst_ro: read-only, no raw event payloads.
GRANT SELECT ON ledger_transactions, analyst_webhook_events, recon_runs, recon_matches, recon_breaks TO analyst_ro;
