-- 002_ingestion.sql
-- Webhook intake. Processors deliver events AT LEAST ONCE, so we must be able to
-- see the same event twice and still do the right thing.

CREATE TABLE webhook_events (
    event_id      TEXT PRIMARY KEY,                     -- the processor's event id: the dedupe key
    event_type    TEXT NOT NULL,
    payload       JSONB NOT NULL,                       -- stored AFTER PII redaction
    status        TEXT NOT NULL DEFAULT 'received'
                  CHECK (status IN ('received', 'processed', 'ignored', 'dead_letter')),
    received_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at  TIMESTAMPTZ
);

CREATE INDEX idx_webhook_events_received_at ON webhook_events (received_at);

-- Events we could not process. Kept so a human can fix the cause and replay.
CREATE TABLE dead_letter_events (
    id           BIGSERIAL PRIMARY KEY,
    event_id     TEXT NOT NULL,
    payload      JSONB,
    error        TEXT NOT NULL,
    failed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    retry_count  INT NOT NULL DEFAULT 0,
    resolved_at  TIMESTAMPTZ
);

CREATE INDEX idx_dead_letter_open ON dead_letter_events (event_id) WHERE resolved_at IS NULL;
