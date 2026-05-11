BEGIN;

CREATE TABLE IF NOT EXISTS "SyncJob" (
    "JobID"        SERIAL PRIMARY KEY,
    "TenantID"     INTEGER REFERENCES "Tenant"("TenantID") DEFAULT 1,
    "UserID"       INTEGER REFERENCES "Users"("UserID"),
    "TriggeredBy"  INTEGER REFERENCES "Users"("UserID"),
    "Source"       VARCHAR(32) NOT NULL,
    "Status"       VARCHAR(32) NOT NULL DEFAULT 'queued',
    "StartedAt"    TIMESTAMPTZ,
    "FinishedAt"   TIMESTAMPTZ,
    "PapersAdded"  INTEGER,
    "ErrorMessage" TEXT,
    "Metadata"     JSONB DEFAULT '{}'::jsonb,
    "CreatedAt"    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_syncjob_user ON "SyncJob"("UserID");
CREATE INDEX IF NOT EXISTS idx_syncjob_status ON "SyncJob"("Status");

COMMIT;
