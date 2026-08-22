-- =============================================================================
-- Sprint 11 — Duplicate-Paper Merge Approval
-- =============================================================================
-- Adds the MergeApproval table designed in Phase 4H
-- (backend/reports/phase4h_approval_storage_design.md) and modeled directly
-- on this repository's one real, proven human-decision-queue precedent,
-- sprint8_author_review_queue.sql's AuthorReviewQueue.
--
-- THIS FILE IS A SCHEMA ARTIFACT ONLY -- Phase 4I (its implementation phase)
-- deliberately did NOT run it against any database, production or otherwise.
-- See backend/reports/phase4i_merge_approval_prototype.md for why: every
-- prior phase of this project maintained zero DB writes, and inserting rows
-- (let alone creating a table) was explicitly gated behind separate,
-- explicit future approval in the Phase 4I task itself. This file exists so
-- a future, explicitly-approved phase can apply it with
-- backend/apply_migration.py exactly as every other file in this directory
-- is applied -- no new tooling required.
--
-- Phase 4K.1 CORRECTION -- LoserPaperID's foreign key was removed entirely.
-- Phase 4K (backend/reports/phase4k_live_execution_readiness_audit.md, §4)
-- proved with certainty that this table's original design (both
-- SurvivorPaperID and LoserPaperID as ON DELETE NO ACTION) was
-- self-contradictory: an APPROVED MergeApproval row is the unconditional
-- precondition for reaching execution, and that same row's LoserPaperID
-- reference (undeferred NO ACTION) makes Postgres refuse to DELETE the
-- loser ResearchPaper row while it exists -- so no approved merge could
-- ever actually complete. See backend/reports/phase4k1_fk_lifecycle_fix.md
-- for the full option evaluation (SET NULL and CASCADE were both
-- evaluated and rejected -- SET NULL silently loses the merged pair's
-- identity from this column at exactly the moment it matters most;
-- CASCADE would delete the audit row this table exists to keep) and the
-- live precedent (AuditLog.TargetID, confirmed to carry no FK constraint
-- at all) this fix is modeled on directly.
--
-- Why this shape
-- --------------
--   * SurvivorPaperID / LoserPaperID are two separate, required FK columns
--     (not one PaperID + a "role" flag) -- Phase 4H's explicit finding that
--     "5232 survives / 5482 loses is NOT equivalent to the reverse" means
--     direction must be structurally unambiguous, not inferable.
--   * PlanFingerprint is the actual safety-binding column (Phase 4G's
--     compute_plan_fingerprint() output, a SHA-256 hex digest) -- an
--     approval is valid only for the exact data state it was reviewed
--     against; regenerating the plan from UNCHANGED data reproduces the
--     identical fingerprint (proven across 4+ live runs on 2 different
--     days), so a plan can be freely regenerated without ever invalidating
--     an existing approval, while any load-bearing field's real drift
--     produces a different one and DOES invalidate it.
--   * ApprovalVersion is never mutated in place for a fingerprint change or
--     a fresh review cycle after REJECTED/REVOKED/EXECUTED -- a new row
--     (higher version) is created instead, matching the immutable-artifact
--     design already established in merge_execution_safety.py's
--     ApprovalArtifact dataclass.
--   * Status is a closed enum via CHECK constraint, exactly like
--     AuthorReviewQueue.Status -- no free text.
--   * ReviewedByUserID / ReviewedAt / ReviewerNotes is the same
--     reviewer-audit triplet AuthorReviewQueue already uses, unchanged.
--   * RevokedByUserID / RevokedAt / RevocationReason mirrors that triplet
--     for the one state transition AuthorReviewQueue's simpler domain never
--     needed -- retracting a decision before an irreversible, hard-to-undo
--     action (a merge deletes a ResearchPaper row; AuthorReviewQueue's own
--     decisions are cheaply-corrected Authors links).
--   * ExecutedAt / ExecutionAuditLogID link this approval to the real
--     AuditLog row a future executor writes -- AuditLog remains the sole
--     authoritative source for "did the merge actually happen"
--     (Phase 4G/4H); these columns are a derived cross-reference, never a
--     second source of truth for that fact.
--   * SurvivorPaperID uses ON DELETE NO ACTION, deliberately DIFFERENT
--     from AuthorReviewQueue's ON DELETE CASCADE on its own PaperID
--     column -- an AuthorReviewQueue row is meaningless once its paper is
--     gone, but a MergeApproval row remains meaningful audit history even
--     after a successful merge. The survivor is never deleted by any
--     designed code path, so NO ACTION here is a pure safety net: if a
--     future bug ever attempted to delete a row that is actually someone's
--     recorded survivor, Postgres refuses loudly rather than silently
--     orphaning the approval's meaning.
--   * LoserPaperID carries NO foreign key at all (Phase 4K.1 correction --
--     see above). The loser row IS deleted, by design, by every successful
--     merge -- unlike the survivor, an ON DELETE NO ACTION (or any other
--     FK) on this column is fundamentally incompatible with the table's
--     own stated purpose of surviving that exact deletion. This mirrors
--     AuditLog.TargetID exactly (live-confirmed, Phase 4K.1: no FK
--     constraint, TenantID/UserID are the table's only FKs), the one real,
--     already-production-proven precedent in this schema for "a reference
--     that must outlive the row it names." The tradeoff, accepted
--     explicitly and matching AuditLog's own accepted tradeoff: this
--     column's value is no longer validated against ResearchPaper at
--     INSERT time. This does not weaken execution-time safety --
--     execute_approved_merge()'s own preflight (merge_execution_safety.py's
--     fetch_current_state(), re-run fresh under lock) already independently
--     re-verifies both PaperIDs are real, live rows before any write can
--     occur, regardless of what this FK would or wouldn't have checked at
--     approval-creation time.
-- =============================================================================

CREATE TABLE IF NOT EXISTS "MergeApproval" (
    "ApprovalID"           SERIAL          PRIMARY KEY,
    "SurvivorPaperID"      INT             NOT NULL REFERENCES "ResearchPaper"("PaperID") ON DELETE NO ACTION,
    "LoserPaperID"         INT             NOT NULL,
    "PlanID"               VARCHAR(64)     NOT NULL,
    "PlanFingerprint"      VARCHAR(64)     NOT NULL,
    "ApprovalVersion"      INT             NOT NULL DEFAULT 1,
    "TenantID"             INT             NOT NULL REFERENCES "Tenant"("TenantID"),
    "Status"               VARCHAR(20)     NOT NULL DEFAULT 'PENDING'
                            CHECK ("Status" IN ('PENDING','APPROVED','REJECTED','REVOKED','EXECUTED')),
    "ReviewedByUserID"     INT             NULL     REFERENCES "Users"("UserID") ON DELETE SET NULL,
    "ReviewedAt"           TIMESTAMPTZ     NULL,
    "ReviewerNotes"        TEXT            NULL,
    "RevokedByUserID"      INT             NULL     REFERENCES "Users"("UserID") ON DELETE SET NULL,
    "RevokedAt"            TIMESTAMPTZ     NULL,
    "RevocationReason"     TEXT            NULL,
    "ExecutedAt"           TIMESTAMPTZ     NULL,
    "ExecutionAuditLogID"  INT             NULL     REFERENCES "AuditLog"("LogID") ON DELETE SET NULL,
    "PlanSnapshotJSON"     JSONB           NULL,
    "CreatedAt"            TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    -- A self-merge is never a valid row -- enforced at the schema level as
    -- a second, independent guard beneath the application-level
    -- reject_self_merge() check (merge_execution_safety.py, reused
    -- unmodified by Phase 4I's approval-operation functions).
    CONSTRAINT chk_merge_approval_not_self CHECK ("SurvivorPaperID" != "LoserPaperID"),

    -- At most one row per (pair identity, exact fingerprint, version) --
    -- the deterministic handling for duplicate-approval creation Phase 4I
    -- requires: create_pending_approval() looks this up first and returns
    -- the existing PENDING/APPROVED row rather than racing this constraint.
    CONSTRAINT uq_merge_approval_identity UNIQUE
        ("SurvivorPaperID", "LoserPaperID", "PlanFingerprint", "ApprovalVersion")
);

CREATE INDEX IF NOT EXISTS idx_merge_approval_status_created
    ON "MergeApproval" ("Status", "CreatedAt" DESC);

CREATE INDEX IF NOT EXISTS idx_merge_approval_lookup
    ON "MergeApproval" ("SurvivorPaperID", "LoserPaperID", "PlanFingerprint");
