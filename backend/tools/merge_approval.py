"""Phase 4I -- MergeApproval prototype implementation.

Implements ONLY the approval-storage layer designed in Phase 4H
(backend/reports/phase4h_approval_storage_design.md), against the
MergeApproval table defined in
backend/analytics/migrations/sprint11_merge_approval.sql. That migration
file has NOT been applied to any database by this phase (see the phase
report) -- every function here issues SQL that assumes the table exists,
but this module itself never creates it and is never called against a real
connection during the automated test suite (mocked cursor only).

Modeled directly on backend/analytics/reconciliation_views.py's real,
working AuthorReviewQueue confirm/reject/skip endpoint -- same
SELECT ... FOR UPDATE + illegal-transition guard + audit()-outside-the-
cursor-block pattern, same permission-check shape, applied to a new
domain rather than reinvented.

Hard boundary, by design (see test_merge_approval.py's static guard tests):
  - No merge_group() import or call.
  - No DELETE against "ResearchPaper".
  - No DOI-column writes of any kind.
  - No child-table remapping, JournalID backfill, or AuthorNameRaw
    conflict-resolution logic -- this module only records a human's
    decision about a plan; it never acts on the plan's contents.
  - "ExecutedAt"/"ExecutionAuditLogID" are schema columns this module can
    read (fetch_current_approval()) but this module contains no function
    that writes them -- setting them is a future executor's job (Phase 4H
    §H/§I design), explicitly out of scope here (see the "D. Explicit
    Non-Goals" section of the phase report).
"""
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR / "tools"))

# Reused, not reimplemented -- per this phase's explicit instruction not to
# invent an abstraction the repository already has. Phase 4G's
# reject_self_merge() is the exact, already-tested check this module needs.
# Phase 4U's validate_same_tenant()/fetch_paper_tenant_ids() close a real,
# confirmed cross-tenant enforcement gap (see
# backend/reports/phase4u_cross_tenant_enforcement.md) -- neither existed
# before this phase.
from merge_execution_safety import (  # noqa: E402
    fetch_paper_tenant_ids,
    reject_self_merge,
    validate_same_tenant,
)

# ---------------------------------------------------------------------------
# Status model -- MUST match sprint11_merge_approval.sql's CHECK constraint
# exactly, string-for-string.
# ---------------------------------------------------------------------------
STATUS_PENDING = "PENDING"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"
STATUS_REVOKED = "REVOKED"
STATUS_EXECUTED = "EXECUTED"

ALL_STATUSES = {STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED, STATUS_REVOKED, STATUS_EXECUTED}

# Phase 4H §G's minimum safe lifecycle, encoded directly: PENDING branches to
# APPROVED or REJECTED; APPROVED branches to REVOKED or EXECUTED (EXECUTED is
# reachable in the schema/model but no function in this module transitions
# to it -- that is a future executor's job, see the module docstring).
# REJECTED/REVOKED/EXECUTED are all terminal for that row (a fresh review
# cycle creates a NEW row/version -- see create_pending_approval()).
LEGAL_TRANSITIONS = {
    STATUS_PENDING: {STATUS_APPROVED, STATUS_REJECTED},
    STATUS_APPROVED: {STATUS_REVOKED, STATUS_EXECUTED},
    STATUS_REJECTED: set(),
    STATUS_REVOKED: set(),
    STATUS_EXECUTED: set(),
}


def is_legal_transition(current_status, target_status):
    """Pure function. True iff current_status -> target_status is a
    permitted edge in the Phase 4H state model."""
    return target_status in LEGAL_TRANSITIONS.get(current_status, set())


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------

APPROVAL_COLUMNS = [
    "ApprovalID", "SurvivorPaperID", "LoserPaperID", "PlanID", "PlanFingerprint",
    "ApprovalVersion", "TenantID", "Status", "ReviewedByUserID", "ReviewedAt",
    "ReviewerNotes", "RevokedByUserID", "RevokedAt", "RevocationReason",
    "ExecutedAt", "ExecutionAuditLogID", "CreatedAt",
]


@dataclass(frozen=True)
class ApprovalRow:
    approval_id: int
    survivor_paper_id: int
    loser_paper_id: int
    plan_id: str
    plan_fingerprint: str
    approval_version: int
    tenant_id: int
    status: str
    reviewed_by_user_id: Optional[int]
    reviewed_at: object
    reviewer_notes: Optional[str]
    revoked_by_user_id: Optional[int]
    revoked_at: object
    revocation_reason: Optional[str]
    executed_at: object
    execution_audit_log_id: Optional[int]
    created_at: object


@dataclass(frozen=True)
class OperationResult:
    ok: bool
    error: Optional[str] = None
    approval: Optional[ApprovalRow] = None


def _row_to_approval(row):
    if row is None:
        return None
    d = dict(zip(APPROVAL_COLUMNS, row))
    return ApprovalRow(
        approval_id=d["ApprovalID"], survivor_paper_id=d["SurvivorPaperID"],
        loser_paper_id=d["LoserPaperID"], plan_id=d["PlanID"],
        plan_fingerprint=d["PlanFingerprint"], approval_version=d["ApprovalVersion"],
        tenant_id=d["TenantID"], status=d["Status"],
        reviewed_by_user_id=d["ReviewedByUserID"], reviewed_at=d["ReviewedAt"],
        reviewer_notes=d["ReviewerNotes"], revoked_by_user_id=d["RevokedByUserID"],
        revoked_at=d["RevokedAt"], revocation_reason=d["RevocationReason"],
        executed_at=d["ExecutedAt"], execution_audit_log_id=d["ExecutionAuditLogID"],
        created_at=d["CreatedAt"],
    )


# ---------------------------------------------------------------------------
# Permission check -- mirrors reconciliation_views.py::_can_reconcile()
# exactly: same permission code (manage_users), same Admin fallback. Phase
# 4H's own investigation found no more specific existing permission code for
# merge-approval; reusing manage_users repeats the established convention
# (AuthorReviewQueue's own justification -- "author linkage is a user-data
# integrity operation" -- applies just as directly to paper-record
# integrity) rather than inventing a new, undecided permission code this
# phase has no product mandate to create.
# ---------------------------------------------------------------------------

def _has_perm(user, code):
    checker = getattr(user, "has_litrix_perm", None)
    return bool(checker and checker(code))


def can_approve_merge(user):
    if not getattr(user, "is_authenticated", True):
        return False
    return _has_perm(user, "manage_users") or getattr(user, "user_type", None) == "Admin"


# ---------------------------------------------------------------------------
# Audit integration -- reuses accounts/common.py::audit() exactly, the same
# shared helper the real AuthorReviewQueue decide-endpoint calls. Per
# repository evidence (backend/analytics/disambiguation/pipeline.py, the
# real AuthorReviewQueue row-creation path, contains ZERO audit() calls --
# grep-confirmed this phase): creation is NOT audited by the real precedent,
# only the human DECISION is. This module follows that exactly: no audit
# call in create_pending_approval(); approve/reject/revoke each audit.
# ---------------------------------------------------------------------------

def _write_audit(user, tenant_id, action, target_id, metadata):
    from accounts.common import audit
    audit(
        user_id=getattr(user, "user_id", None),
        tenant_id=tenant_id,
        action=action,
        target_type="MergeApproval",
        target_id=target_id,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Read-only lookup
# ---------------------------------------------------------------------------

def fetch_current_approval(cur, survivor_id, loser_id, plan_fingerprint):
    """Read-only, no lock. Returns the highest-ApprovalVersion row for this
    EXACT (survivor, loser, fingerprint) identity, or None. Because the
    lookup is scoped by plan_fingerprint in the WHERE clause itself, an
    approval recorded for one fingerprint is structurally unreachable when
    looking up a different one -- 'silent reuse across a changed plan' is
    not just disallowed by convention, it cannot occur via this query."""
    cols_sql = ', '.join(f'"{c}"' for c in APPROVAL_COLUMNS)
    cur.execute(
        f'SELECT {cols_sql} FROM "MergeApproval" '
        f'WHERE "SurvivorPaperID" = %s AND "LoserPaperID" = %s AND "PlanFingerprint" = %s '
        f'ORDER BY "ApprovalVersion" DESC LIMIT 1',
        (survivor_id, loser_id, plan_fingerprint),
    )
    return _row_to_approval(cur.fetchone())


def approval_matches_pair(approval, survivor_id, loser_id):
    """Pure function. True only if the approval's recorded direction
    exactly matches (survivor_id, loser_id) -- a swapped pair never
    matches, regardless of fingerprint. This is the approval-layer
    equivalent of validate_against_plan()'s PREFLIGHT_REVERSED check
    (merge_execution_safety.py), applied to a stored approval rather than a
    freshly-generated plan."""
    if approval is None:
        return False
    return approval.survivor_paper_id == survivor_id and approval.loser_paper_id == loser_id


# ---------------------------------------------------------------------------
# 1. Create a PENDING approval
# ---------------------------------------------------------------------------

def create_pending_approval(cur, user, survivor_id, loser_id, plan_id, plan_fingerprint,
                             tenant_id, plan_snapshot_json=None):
    """Rejects a self-merge before issuing any SQL (reject_self_merge(),
    reused). Deterministic duplicate handling: if an active (PENDING or
    APPROVED) row already exists for this exact (survivor, loser,
    fingerprint), returns THAT row rather than creating a second one --
    idempotent, not a race. If the most recent row for this identity is
    already EXECUTED, refuses to create a new one (nothing left to approve
    -- the pair should no longer exist). Otherwise creates a new row at
    ApprovalVersion = (max seen for this exact identity) + 1, so a fresh
    review cycle after REJECTED/REVOKED never collides with the unique
    constraint and never silently mutates the terminal history it
    supersedes."""
    ok, reason = reject_self_merge(survivor_id, loser_id)
    if not ok:
        return OperationResult(ok=False, error=f"self_merge_rejected: {reason}")

    if not can_approve_merge(user):
        return OperationResult(ok=False, error="permission_denied")

    # Phase 4U: cross-tenant guard, enforcement boundary #1 of 2 (the other
    # is execute_approved_merge() itself). A cross-tenant pair must never
    # reach an INSERT here -- checked before the dedup lookup below so an
    # invalid pair costs one query, not two, and definitely before any SQL
    # that could create a row.
    survivor_tenant_id, loser_tenant_id = fetch_paper_tenant_ids(cur, survivor_id, loser_id)
    ok, reason = validate_same_tenant(survivor_tenant_id, loser_tenant_id)
    if not ok:
        return OperationResult(ok=False, error=f"cross_tenant_rejected: {reason}")

    existing = fetch_current_approval(cur, survivor_id, loser_id, plan_fingerprint)
    if existing is not None:
        if existing.status in (STATUS_PENDING, STATUS_APPROVED):
            return OperationResult(ok=True, approval=existing)
        if existing.status == STATUS_EXECUTED:
            return OperationResult(ok=False, error="already_executed")

    cur.execute(
        'SELECT COALESCE(MAX("ApprovalVersion"), 0) FROM "MergeApproval" '
        'WHERE "SurvivorPaperID" = %s AND "LoserPaperID" = %s AND "PlanFingerprint" = %s',
        (survivor_id, loser_id, plan_fingerprint),
    )
    next_version = (cur.fetchone()[0] or 0) + 1

    cols_sql = ', '.join(f'"{c}"' for c in APPROVAL_COLUMNS)
    cur.execute(
        f'''INSERT INTO "MergeApproval"
                ("SurvivorPaperID","LoserPaperID","PlanID","PlanFingerprint",
                 "ApprovalVersion","TenantID","Status","PlanSnapshotJSON")
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            RETURNING {cols_sql}''',
        (survivor_id, loser_id, plan_id, plan_fingerprint, next_version, tenant_id,
         STATUS_PENDING, plan_snapshot_json),
    )
    approval = _row_to_approval(cur.fetchone())
    return OperationResult(ok=True, approval=approval)


# ---------------------------------------------------------------------------
# 2/3/4. State transitions -- approve / reject / revoke
# ---------------------------------------------------------------------------

def _transition(cur, user, approval_id, expected_plan_fingerprint, target_status,
                 status_columns_sql, status_params, action, metadata_extra=None):
    """Shared machinery for approve/reject/revoke -- SELECT ... FOR UPDATE,
    verify legal transition, verify fingerprint match, UPDATE, audit()
    outside the cursor block (matching reconciliation_views.py's own
    comment: 'audit() opens its own cursor'). Caller supplies the exact
    SET-clause SQL/params for the fields specific to that transition."""
    cols_sql = ', '.join(f'"{c}"' for c in APPROVAL_COLUMNS)
    cur.execute(
        f'SELECT {cols_sql} FROM "MergeApproval" WHERE "ApprovalID" = %s FOR UPDATE',
        (approval_id,),
    )
    row = cur.fetchone()
    if row is None:
        return OperationResult(ok=False, error="not_found")

    current = _row_to_approval(row)

    if not can_approve_merge(user):
        return OperationResult(ok=False, error="permission_denied")

    if not is_legal_transition(current.status, target_status):
        return OperationResult(
            ok=False,
            error=f"illegal_transition: {current.status} -> {target_status}",
            approval=current,
        )

    # Never allow an approval decision to be silently applied to a DIFFERENT
    # PlanFingerprint than the one the reviewer actually saw -- the caller
    # must supply the fingerprint they believe they are deciding on, and it
    # must exactly match what is stored.
    if current.plan_fingerprint != expected_plan_fingerprint:
        return OperationResult(
            ok=False,
            error="fingerprint_mismatch: this approval no longer matches the reviewed plan",
            approval=current,
        )

    cur.execute(
        f'UPDATE "MergeApproval" SET {status_columns_sql} WHERE "ApprovalID" = %s '
        f'RETURNING {cols_sql}',
        status_params + (approval_id,),
    )
    updated = _row_to_approval(cur.fetchone())

    _write_audit(
        user, current.tenant_id, action, approval_id,
        {
            "survivorPaperId": current.survivor_paper_id,
            "loserPaperId": current.loser_paper_id,
            "planFingerprint": current.plan_fingerprint,
            "fromStatus": current.status,
            "toStatus": target_status,
            **(metadata_extra or {}),
        },
    )
    return OperationResult(ok=True, approval=updated)


def approve_pending(cur, user, approval_id, expected_plan_fingerprint, notes=None):
    """PENDING -> APPROVED."""
    return _transition(
        cur, user, approval_id, expected_plan_fingerprint, STATUS_APPROVED,
        '"Status" = %s, "ReviewedByUserID" = %s, "ReviewedAt" = NOW(), "ReviewerNotes" = %s',
        (STATUS_APPROVED, getattr(user, "user_id", None), notes),
        action="merge_approval.approved",
        metadata_extra={"notes": notes},
    )


def reject_pending(cur, user, approval_id, expected_plan_fingerprint, notes=None):
    """PENDING -> REJECTED."""
    return _transition(
        cur, user, approval_id, expected_plan_fingerprint, STATUS_REJECTED,
        '"Status" = %s, "ReviewedByUserID" = %s, "ReviewedAt" = NOW(), "ReviewerNotes" = %s',
        (STATUS_REJECTED, getattr(user, "user_id", None), notes),
        action="merge_approval.rejected",
        metadata_extra={"notes": notes},
    )


def revoke_approved(cur, user, approval_id, expected_plan_fingerprint, reason=None):
    """APPROVED -> REVOKED. Only legal before execution -- an already
    EXECUTED approval has no REVOKED edge in LEGAL_TRANSITIONS, so this
    correctly refuses (illegal_transition) rather than trying to
    retroactively revoke a merge that already happened."""
    return _transition(
        cur, user, approval_id, expected_plan_fingerprint, STATUS_REVOKED,
        '"Status" = %s, "RevokedByUserID" = %s, "RevokedAt" = NOW(), "RevocationReason" = %s',
        (STATUS_REVOKED, getattr(user, "user_id", None), reason),
        action="merge_approval.revoked",
        metadata_extra={"reason": reason},
    )
