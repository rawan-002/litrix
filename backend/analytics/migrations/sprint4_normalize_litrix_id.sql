-- ============================================================================
-- Sprint 4 — Normalize Litrix_ID to canonical Lit-NNNNNN (deterministic)
-- ============================================================================
-- Why a full renumber instead of in-place case fix?
--   Sprint 3's backfill used a case-sensitive MAX(...) so it didn't see
--   the legacy "LIT-NNNNNN" rows; new assignments collided in numeric
--   space (LIT-000001 vs Lit-000001 → same canonical → UNIQUE violation
--   on plain UPDATE).
--
--   Cleanest resolution: renumber ALL rows in UserID order to a single
--   contiguous sequence. Result:
--       Lit-000001  ←  oldest user
--       Lit-000002  ←  next
--       ...
--       Lit-000108  ←  newest user
--
--   Side effect: legacy LIT-XXXXXX numbers change. Acceptable here —
--   the IDs were never published externally, and this is the one-time
--   moment to lock the schema down for SaaS stability going forward.
--
-- Strategy (two-phase to dodge UNIQUE on intermediate states):
--   Phase 1: park every Litrix-shaped row in a temporary unique
--            namespace (TMP-<userid>) so no collisions are possible.
--   Phase 2: assign canonical Lit-NNNNNN by UserID order.
--
-- Idempotent — safe to re-run.
-- ============================================================================

BEGIN;

-- Pause the auto-assign trigger so it doesn't fight us during the
-- two-phase swap.
ALTER TABLE "Users" DISABLE TRIGGER trg_assign_litrix_id;

-- ----------------------------------------------------------------------------
-- Phase 1 — park all Litrix-ID-shaped rows in a per-user temporary value.
--   Uses UserID as the suffix to guarantee uniqueness during the swap.
-- ----------------------------------------------------------------------------
UPDATE "Users"
   SET "Litrix_ID" = 'TMP-' || "UserID"::TEXT
 WHERE "Litrix_ID" ~* '^lit-[0-9]+$';

-- ----------------------------------------------------------------------------
-- Phase 2 — re-assign canonical Lit-NNNNNN deterministically by UserID.
--   ROW_NUMBER guarantees a contiguous 1..N sequence with no gaps and no
--   reliance on the (possibly sparse) UserID values.
-- ----------------------------------------------------------------------------
WITH ordered AS (
    SELECT "UserID",
           ROW_NUMBER() OVER (ORDER BY "UserID") AS seq
      FROM "Users"
     WHERE "Litrix_ID" LIKE 'TMP-%'
)
UPDATE "Users" u
   SET "Litrix_ID" = 'Lit-' || LPAD(o.seq::TEXT, 6, '0')
  FROM ordered o
 WHERE u."UserID" = o."UserID";

ALTER TABLE "Users" ENABLE TRIGGER trg_assign_litrix_id;

COMMIT;
