-- ============================================================================
-- Sprint 3 — Litrix_ID public identifier
-- ============================================================================
-- Why a separate public ID alongside UserID?
--   • UserID (SERIAL PK) drives FK joins — fast, internal-only.
--   • Litrix_ID (Lit-NNNNNN) is the stable, human-friendly identifier
--     surfaced in URLs, sharing, and Author-disambiguation references.
--   • Decoupling them protects URLs from PK churn (re-imports, merges,
--     tenant migrations) and keeps internal IDs out of public surfaces.
--
-- This migration is idempotent — safe to re-run.
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- 1. Backfill: assign Lit-NNNNNN to every existing user that doesn't have one.
--    Order by UserID so the assignment is deterministic and matches creation
--    order — Lit-000001 = first user, Lit-000002 = second, …
-- ----------------------------------------------------------------------------
WITH numbered AS (
    SELECT
        "UserID",
        -- start the sequence after any existing manually-assigned values
        ROW_NUMBER() OVER (ORDER BY "UserID")
            + COALESCE((
                SELECT MAX(CAST(SUBSTRING("Litrix_ID" FROM 5) AS INTEGER))
                FROM "Users"
                WHERE "Litrix_ID" ~ '^Lit-[0-9]+$'
            ), 0)
            AS seq
    FROM "Users"
    WHERE "Litrix_ID" IS NULL OR "Litrix_ID" = ''
)
UPDATE "Users" u
   SET "Litrix_ID" = 'Lit-' || LPAD(numbered.seq::TEXT, 6, '0')
  FROM numbered
 WHERE u."UserID" = numbered."UserID";

-- ----------------------------------------------------------------------------
-- 2. Uniqueness constraint — guards against accidental duplicates.
--    DROP-then-ADD pattern keeps the migration idempotent.
-- ----------------------------------------------------------------------------
ALTER TABLE "Users" DROP CONSTRAINT IF EXISTS users_litrix_id_unique;
ALTER TABLE "Users"
    ADD CONSTRAINT users_litrix_id_unique UNIQUE ("Litrix_ID");

-- ----------------------------------------------------------------------------
-- 3. Auto-generation trigger — ANY insert path (ORM, raw SQL, manual psql)
--    that omits Litrix_ID gets one auto-assigned. Defense in depth.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_assign_litrix_id()
RETURNS TRIGGER AS $$
DECLARE
    next_seq INTEGER;
BEGIN
    IF NEW."Litrix_ID" IS NOT NULL AND NEW."Litrix_ID" <> '' THEN
        RETURN NEW;
    END IF;

    -- pg_advisory_xact_lock serializes concurrent inserts so two new users
    -- can't grab the same sequence number under contention.
    PERFORM pg_advisory_xact_lock(hashtext('litrix_id_seq'));

    SELECT COALESCE(
            MAX(CAST(SUBSTRING("Litrix_ID" FROM 5) AS INTEGER)),
            0
        ) + 1
      INTO next_seq
      FROM "Users"
     WHERE "Litrix_ID" ~ '^Lit-[0-9]+$';

    NEW."Litrix_ID" := 'Lit-' || LPAD(next_seq::TEXT, 6, '0');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_assign_litrix_id ON "Users";
CREATE TRIGGER trg_assign_litrix_id
    BEFORE INSERT ON "Users"
    FOR EACH ROW
    EXECUTE FUNCTION fn_assign_litrix_id();

-- ----------------------------------------------------------------------------
-- 4. Make the column NOT NULL — every user must have an identifier.
-- ----------------------------------------------------------------------------
ALTER TABLE "Users" ALTER COLUMN "Litrix_ID" SET NOT NULL;

-- ----------------------------------------------------------------------------
-- 5. Index for the most common lookup path (URL → user). The unique
--    constraint already creates an implicit index, but we expose an
--    explicit named one for clarity in EXPLAIN plans.
-- ----------------------------------------------------------------------------
-- (skipped — UNIQUE already creates the index)

COMMIT;
