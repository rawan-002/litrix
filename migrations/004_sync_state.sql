


BEGIN;

ALTER TABLE "Researcher"
    ADD COLUMN IF NOT EXISTS "LastSyncedAt" TIMESTAMPTZ;


CREATE INDEX IF NOT EXISTS "ix_researcher_last_synced"
    ON "Researcher" ("LastSyncedAt" NULLS FIRST);

COMMIT;


