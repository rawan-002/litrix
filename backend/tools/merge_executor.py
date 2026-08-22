"""Phase 4J -- Merge Executor Prototype. Implements the ONE remaining piece
Phase 4F/4G/4H/4I left unbuilt: the actual code path that, given an exact
APPROVED MergeApproval identity, re-validates everything live and then
performs the real merge -- child remaps, the JournalID backfill Phase 4E
designed but never wired in, the AuditLog record, marking the approval
EXECUTED, and deleting the loser -- all inside one transaction.

NO LIVE EXECUTION happened to produce this file. Every behavior below is
validated exclusively against ExecutorFakeCursor (test_merge_executor.py),
never a real psycopg2/Django connection. See the module docstring's "Hard
boundary" section for the automated, static proof of this.

Design principle, reused unmodified from every prior phase: reuse, don't
rewrite. This module is orchestration ONLY -- every actual safety check and
every actual write already exists elsewhere, tested, and is imported here:
  - merge_execution_safety.py: reject_self_merge, lock_pair_rows,
    fetch_current_state, compute_plan_fingerprint, validate_against_plan,
    is_doi_claimed_elsewhere, fetch_merge_audit_rows, idempotency_verdict.
  - merge_approval.py: fetch_current_approval, approval_matches_pair,
    can_approve_merge, is_legal_transition, the STATUS_* constants.
  - dedup_papers.py: merge_group (UNMODIFIED -- see below), pair_confidence,
    hard_exclusion_reason, author_content_conflicts, journal_id_decision,
    existing_child_tables, authors_columns.
  - merge_plan_generator.py: build_journal_state,
    build_papers_dict_for_pure_functions.

The only genuinely NEW logic in this file is: (1) orchestrating the above
in the exact order the task specifies, (2) the JournalID backfill UPDATE
(Phase 4E designed this action, no code ever applied it), (3) the
live re-check of the two dependency gaps Phase 4F/4H flagged as
unaddressed (AuthorReviewQueue CASCADE, ReportPaperDecision.
MissingResolvedToPaperID SET NULL) -- both now BLOCK execution if nonzero
rather than being silently ignored, and (4) the MergeApproval ->
EXECUTED transition itself, which Phase 4I explicitly left unimplemented
as "a future executor's job" (see merge_approval.py's own module
docstring) -- this phase IS that future executor.

merge_group() is deliberately left completely UNMODIFIED. It is real,
tested, production code with 59 historical merges behind it; splitting its
internal AuditLog-write/delete sequence apart to interleave a MergeApproval
update between them would mean editing that trusted code path during a
phase that performs no live execution to validate the edit against. See
"Transaction Sequence, And Why It Diverges From The Literal Task Ordering"
in the phase report for the full reasoning -- the ordering used here
(remaps -> AuditLog -> delete, all inside one unmodified merge_group()
call -> THEN mark MergeApproval EXECUTED) matches Phase 4H's own approved
executor design (report Section H) rather than this phase's own request
text verbatim, and that divergence is a deliberate, flagged choice, not an
oversight.

Hard boundary, by design (see test_merge_executor.py's static guard tests):
  - No network client of any kind exists in this file.
  - This module never opens a transaction, and never commits or rolls one
    back -- exactly like lock_pair_rows() and every write-issuing function
    in merge_approval.py, the CALLER owns the transaction. Any exception
    raised here (including merge_group()'s own
    RuntimeError on a profile-preservation violation) must propagate
    unhandled to the caller's atomic-transaction context so the whole
    operation rolls back -- nothing here ever catches and swallows one.
  - Every write statement below is preceded by every preflight check in
    REQUIRED PRE-FLIGHT ORDER passing -- proven by test scenarios B-J,
    each asserting zero write SQL was issued.
  - execute_approved_merge() cannot reach a single write statement without
    first successfully fetching an APPROVED MergeApproval row that matches
    the exact (survivor, loser) direction -- there is no code path, flag,
    or parameter that bypasses this.
"""
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR / "tools"))

import merge_approval as ma  # noqa: E402
from merge_approval import (  # noqa: E402
    approval_matches_pair,
    can_approve_merge,
    fetch_current_approval,
    is_legal_transition,
)
from merge_execution_safety import (  # noqa: E402
    IDEMPOTENCY_ALREADY_EXECUTED,
    IDEMPOTENCY_HISTORICAL_STATE_AMBIGUOUS,
    MERGE_AUDIT_ACTION,
    fetch_current_state,
    fetch_merge_audit_rows,
    idempotency_verdict,
    is_doi_claimed_elsewhere,
    lock_pair_rows,
    reject_self_merge,
    validate_against_plan,
    validate_same_tenant,
)

# ===========================================================================
# Executor block-reason vocabulary -- one constant per REQUIRED PRE-FLIGHT
# ORDER step (or group of steps) that can refuse execution. Every one of
# these is reachable with ZERO write SQL issued (test scenarios B-J).
# ===========================================================================

EXEC_BLOCKED_SELF_MERGE = "SELF_MERGE"
EXEC_BLOCKED_INVALID_IDENTITY = "INVALID_IDENTITY"
EXEC_BLOCKED_PERMISSION_DENIED = "PERMISSION_DENIED"
EXEC_BLOCKED_NO_APPROVAL = "NO_APPROVAL"
EXEC_BLOCKED_APPROVAL_REVERSED = "APPROVAL_REVERSED"
EXEC_BLOCKED_APPROVAL_NOT_APPROVED = "APPROVAL_NOT_APPROVED"
EXEC_BLOCKED_MISSING_ROW = "MISSING_ROW"
EXEC_BLOCKED_PREFLIGHT_FAILED = "PREFLIGHT_FAILED"
EXEC_BLOCKED_ALREADY_EXECUTED = "ALREADY_EXECUTED"
EXEC_BLOCKED_HISTORY_AMBIGUOUS = "HISTORICAL_STATE_AMBIGUOUS"
EXEC_BLOCKED_JOURNAL_CONFLICT = "JOURNAL_CONFLICT"
EXEC_BLOCKED_AUTHOR_CONFLICT = "AUTHOR_CONFLICT"
EXEC_BLOCKED_DEPENDENCY_GAP = "DEPENDENCY_GAP"
EXEC_BLOCKED_CROSS_TENANT = "CROSS_TENANT"


@dataclass(frozen=True)
class ExecutionResult:
    ok: bool
    blocked_reason: Optional[str] = None
    detail: Optional[dict] = None
    approval_id: Optional[int] = None
    audit_log_id: Optional[int] = None


# ===========================================================================
# FK / dependency action matrix (Section 4 of the task). Data, not prose --
# every dependency Phase 4F's independently-re-derived-three-times 8-row
# table names is classified here as exactly one of REMAP / SET_NULL /
# AUTOMATIC_CASCADE / BLOCK. Only the two entries Phase 4F/4H flagged as
# genuinely unhandled (no remap logic exists anywhere in this repository)
# are classified BLOCK -- everything merge_group() already handles today
# (Authors, Citations, ExternalAuthors, CitationsHistory,
# ReportPaperDecision.PaperID) is classified REMAP and reused unmodified.
# ===========================================================================

DEP_ACTION_REMAP = "REMAP"
DEP_ACTION_SET_NULL = "SET_NULL"
DEP_ACTION_AUTOMATIC_CASCADE = "AUTOMATIC_CASCADE"
DEP_ACTION_BLOCK = "BLOCK"

# (table, fk_column, real ON DELETE rule, executor action, note)
DEPENDENCY_ACTION_MATRIX = [
    ("Authors", "PaperID", "NO ACTION", DEP_ACTION_REMAP,
     "merge_group() special-cased: INSERT ... ON CONFLICT (UserID,PaperID) DO NOTHING, then DELETE loser rows. Reused unmodified."),
    ("Citations", "PaperID", "NO ACTION", DEP_ACTION_REMAP,
     "merge_group() special-cased: GREATEST()-merge of CitationsCount via ON CONFLICT (PaperID) DO UPDATE. Reused unmodified."),
    ("ExternalAuthors", "PaperID", "NO ACTION", DEP_ACTION_REMAP,
     "merge_group()'s SIMPLE_CHILDREN -- remap_simple_child() bulk UPDATE with a per-row SAVEPOINT/conflict-drop fallback. Reused unmodified."),
    ("CitationsHistory", "PaperID", "NO ACTION", DEP_ACTION_REMAP,
     "merge_group()'s SIMPLE_CHILDREN -- same remap_simple_child() path. Reused unmodified."),
    ("ReportPaperDecision", "PaperID", "SET NULL", DEP_ACTION_REMAP,
     "merge_group()'s SIMPLE_CHILDREN remaps this BEFORE the loser row is deleted, so the ON DELETE SET NULL rule is a fallback that should not normally fire. Reused unmodified."),
    ("ReportPaperDecision", "MissingResolvedToPaperID", "SET NULL", DEP_ACTION_BLOCK,
     "Phase 4F gap #2, still real: no remap logic exists anywhere in this repository for this second FK column. check_unhandled_dependency_gaps() live-counts it for the loser and BLOCKs if nonzero, rather than letting it silently SET NULL on delete."),
    ("AuthorReviewQueue", "PaperID", "CASCADE", DEP_ACTION_BLOCK,
     "Phase 4F gap #1, still real: no remap logic exists anywhere in this repository. check_unhandled_dependency_gaps() live-counts it for the loser and BLOCKs if nonzero, rather than letting ON DELETE CASCADE silently destroy rows with zero recovery snapshot."),
]

# The two (table, fk_column) pairs actually re-checked live -- derived from
# the matrix above rather than duplicated as a second literal list.
_LIVE_BLOCK_CHECKS = [
    (table, fk_col) for table, fk_col, _rule, action, _note in DEPENDENCY_ACTION_MATRIX
    if action == DEP_ACTION_BLOCK
]


def check_unhandled_dependency_gaps(cur, loser_id):
    """SELECT-only. Live re-check of the two dependency gaps Phase 4F found
    and Phase 4H/4I both carried forward as unresolved (never assumed zero
    -- always queried fresh, same existing_child_tables()-style
    'table might not exist' guard dedup_papers.py itself already uses).
    Returns a list of blocker dicts; empty means clear to proceed."""
    tables_needed = sorted({table for table, _fk in _LIVE_BLOCK_CHECKS})
    cur.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = ANY(%s)",
        (tables_needed,),
    )
    existing = {r[0] for r in cur.fetchall()}

    blockers = []
    for table, fk_col in _LIVE_BLOCK_CHECKS:
        if table not in existing:
            continue  # matches existing_child_tables()'s convention: a table that doesn't exist has 0 rows by construction
        cur.execute(f'SELECT COUNT(*) FROM "{table}" WHERE "{fk_col}" = %s', (loser_id,))
        count = cur.fetchone()[0]
        if count and count > 0:
            blockers.append({
                "table": table, "foreign_key": fk_col, "row_count": count,
                "reason": f'{count} row(s) in "{table}"."{fk_col}" reference the loser paper, '
                          f'and no remap logic exists for this column -- refusing to guess',
            })
    return blockers


def _identity_valid(survivor_id, loser_id):
    return isinstance(survivor_id, int) and isinstance(loser_id, int) \
        and not isinstance(survivor_id, bool) and not isinstance(loser_id, bool)


def execute_approved_merge(cur, user, survivor_id, loser_id, expected_plan_fingerprint):
    """Given an exact approved merge identity, re-validates everything live
    and -- ONLY if every check in REQUIRED PRE-FLIGHT ORDER passes -- performs
    the real merge inside the caller's already-open transaction.

    Caller MUST already be inside a transaction (Django's own atomic-
    transaction context manager, matching lock_pair_rows()'s and every
    other write-issuing function in this project's own documented
    convention) -- this function never opens, commits, or rolls one back.
    Any exception raised here (including merge_group()'s own RuntimeError
    on a profile-preservation violation, or a deliberately-injected
    failure in a test double) is left to propagate unhandled -- it is the
    caller's transaction context that turns that into a real rollback.

    Returns ExecutionResult(ok=False, blocked_reason=...) with ZERO write
    SQL issued if any preflight check fails. Returns
    ExecutionResult(ok=True, ...) only after every write below has
    succeeded -- the caller must still commit (exiting its transaction
    context normally) for any of it to actually persist.

    "canonical/deterministic survivor-loser validation" (task step 2): this
    function NEVER re-derives who should win via a fresh choose_keep() call
    -- it executes exactly the (survivor_id, loser_id) direction the caller
    supplies, checked only for internal consistency against the stored
    approval (approval_matches_pair) and the live data (validate_against_plan's
    reversed check) -- never re-decided. This matches Phase 4F's explicit
    design principle: execute the approved decision, never re-derive it.
    """
    # --- Step 1: reject self-merge, before any SQL. ---
    ok, reason = reject_self_merge(survivor_id, loser_id)
    if not ok:
        return ExecutionResult(False, EXEC_BLOCKED_SELF_MERGE, {"reason": reason})

    # --- Step 2: canonical/deterministic identity validation. ---
    if not _identity_valid(survivor_id, loser_id):
        return ExecutionResult(False, EXEC_BLOCKED_INVALID_IDENTITY,
                                {"survivor_id": survivor_id, "loser_id": loser_id})

    # Permission check -- reuses the exact mechanism merge_approval.py's own
    # create_pending_approval() checks (manage_users / Admin), in the same
    # position relative to the self-merge check. No distinct "execute"
    # permission exists anywhere in this repository's evidence; inventing
    # one here would be exactly the "invent a new, undecided permission
    # code" this project has repeatedly declined to do without a product
    # mandate (Phase 4H §K, Phase 4I §4, both unchanged).
    if not can_approve_merge(user):
        return ExecutionResult(False, EXEC_BLOCKED_PERMISSION_DENIED)

    # --- Steps 3/4: verify an approval exists, matches direction, is APPROVED. ---
    approval = fetch_current_approval(cur, survivor_id, loser_id, expected_plan_fingerprint)
    if approval is None:
        return ExecutionResult(False, EXEC_BLOCKED_NO_APPROVAL)
    if not approval_matches_pair(approval, survivor_id, loser_id):
        # Defense-in-depth: fetch_current_approval's own WHERE clause already
        # scopes on (SurvivorPaperID, LoserPaperID) exactly equal to the
        # caller's arguments, so a reversed row can never actually be
        # returned here in real use -- this check exists so a future change
        # to that query (or a mocked/monkeypatched cursor in a test) can
        # never silently bypass the direction guarantee, matching the same
        # "two independent checks agreeing on purpose" pattern
        # validate_against_plan() itself already uses.
        return ExecutionResult(False, EXEC_BLOCKED_APPROVAL_REVERSED,
                                {"approval_survivor": approval.survivor_paper_id,
                                 "approval_loser": approval.loser_paper_id})
    if approval.status != ma.STATUS_APPROVED:
        return ExecutionResult(False, EXEC_BLOCKED_APPROVAL_NOT_APPROVED,
                                {"current_status": approval.status})

    # --- Step 5: lock both rows, deterministic ascending order. ---
    locked = lock_pair_rows(cur, survivor_id, loser_id)
    if not locked:
        return ExecutionResult(False, EXEC_BLOCKED_MISSING_ROW)

    # --- Steps 6/7: re-fetch both rows AFTER acquiring the lock. ---
    winner_row, loser_row, winner_authors, loser_authors, w_cit, l_cit = \
        fetch_current_state(cur, survivor_id, loser_id)
    if winner_row is None or loser_row is None:
        return ExecutionResult(False, EXEC_BLOCKED_MISSING_ROW)

    # Phase 4U: cross-tenant guard, enforcement boundary #2 of 2 (the other
    # is create_pending_approval()). Independent of that first check --
    # protects against an approval that somehow predates this fix, or any
    # future caller that bypasses create_pending_approval() entirely.
    # No new query: TenantID is already present on winner_row/loser_row via
    # fetch_current_state()'s existing fetch_paper_row() call.
    tenant_ok, tenant_reason = validate_same_tenant(winner_row.get("TenantID"), loser_row.get("TenantID"))
    if not tenant_ok:
        return ExecutionResult(False, EXEC_BLOCKED_CROSS_TENANT, {"reason": tenant_reason})

    # --- Steps 8/9/13/14: fingerprint + duplicate-safety + DOI-safety, via
    # the exact same validate_against_plan() Phase 4G already built and
    # tested -- not reimplemented. The "plan" it validates against is built
    # from the approval's own recorded fields (survivor/loser/fingerprint):
    # this repository has no separate persisted plan-object store, only
    # MergeApproval rows (see the phase report's explicit note on this). ---
    from dedup_papers import (  # noqa: E402 (local import matches existing convention in merge_execution_safety.py)
        author_content_conflicts, hard_exclusion_reason, pair_confidence,
    )
    from merge_plan_generator import build_journal_state, build_papers_dict_for_pure_functions  # noqa: E402

    papers = build_papers_dict_for_pure_functions(cur, [survivor_id, loser_id])
    pc = pair_confidence(survivor_id, loser_id, papers)
    hard = hard_exclusion_reason(survivor_id, loser_id, papers)
    doi_conflict = is_doi_claimed_elsewhere(cur, winner_row.get("DOI"), [survivor_id, loser_id])

    plan_stub = {
        "survivor": approval.survivor_paper_id,
        "loser": approval.loser_paper_id,
        "plan_fingerprint": approval.plan_fingerprint,
    }
    preflight = validate_against_plan(
        plan_stub, winner_row, loser_row, winner_authors, loser_authors,
        w_cit, l_cit, pc, hard, doi_conflict,
    )
    if not preflight.passed:
        return ExecutionResult(False, EXEC_BLOCKED_PREFLIGHT_FAILED,
                                {"preflight_status": preflight.status,
                                 "reason": preflight.reason, "checks": preflight.checks})

    # --- Steps 10/11/12: idempotency, via the existing 3-way verdict. ---
    audit_rows = fetch_merge_audit_rows(cur, [survivor_id, loser_id])
    verdict, detailed = idempotency_verdict(audit_rows, survivor_id, loser_id)
    if verdict == IDEMPOTENCY_ALREADY_EXECUTED:
        return ExecutionResult(False, EXEC_BLOCKED_ALREADY_EXECUTED, {"reason": detailed.reason})
    if verdict == IDEMPOTENCY_HISTORICAL_STATE_AMBIGUOUS:
        return ExecutionResult(False, EXEC_BLOCKED_HISTORY_AMBIGUOUS, {"reason": detailed.reason})

    # --- Step 15: re-check every plan-required field decision, fresh. ---
    journal_state = build_journal_state(winner_row.get("JournalID"), loser_row.get("JournalID"))
    if not journal_state["execution_permitted"]:
        return ExecutionResult(False, EXEC_BLOCKED_JOURNAL_CONFLICT, {"journal_state": journal_state})

    conflicts = author_content_conflicts(winner_authors, loser_authors)
    if conflicts:
        return ExecutionResult(False, EXEC_BLOCKED_AUTHOR_CONFLICT, {"conflicts": conflicts})

    # --- Step 16: re-check the two previously-flagged, still-unhandled
    # dependency gaps, live -- never assumed zero. ---
    dep_blockers = check_unhandled_dependency_gaps(cur, loser_id)
    if dep_blockers:
        return ExecutionResult(False, EXEC_BLOCKED_DEPENDENCY_GAP, {"blockers": dep_blockers})

    # ======================================================================
    # Every preflight check has passed. No write SQL has been issued above
    # this line. Everything from here on writes.
    # ======================================================================
    from dedup_papers import (  # noqa: E402
        JOURNAL_LOSER_ONLY_BACKFILL, authors_columns, existing_child_tables, merge_group,
    )

    # Deterministic field preservation: the one action Phase 4E designed
    # (LOSER_ONLY_BACKFILL) that merge_group() itself still does not apply.
    # Applied BEFORE merge_group() deletes the loser row, using the
    # loser_row value already fetched under lock -- no second query needed.
    if journal_state["state"] == JOURNAL_LOSER_ONLY_BACKFILL:
        cur.execute(
            'UPDATE "ResearchPaper" SET "JournalID" = %s WHERE "PaperID" = %s',
            (loser_row.get("JournalID"), survivor_id),
        )

    # Child remaps + CitationsByYear merge + the AuditLog write + the loser
    # DELETE -- merge_group(), completely UNMODIFIED (see module docstring
    # for why this file does not split its internals apart).
    child_tables = existing_child_tables(cur)
    a_cols = authors_columns(cur)
    papers_meta = {
        loser_id: {
            "title": loser_row.get("Title") or "",
            "doi": loser_row.get("DOI"),
            "source": loser_row.get("Source"),
            "citations": l_cit,
        }
    }
    merge_group(cur, survivor_id, [loser_id], papers_meta, child_tables, a_cols)
    # merge_group() raises RuntimeError on a profile-preservation violation
    # -- left to propagate unhandled; the caller's transaction rolls back.

    # Locate the AuditLog row merge_group() just wrote, to link it from the
    # approval (ExecutionAuditLogID) -- merge_group() itself has no RETURNING
    # clause on that INSERT and is not modified to add one.
    cur.execute(
        'SELECT "LogID" FROM "AuditLog" WHERE "Action" = %s AND "TargetID" = %s '
        'ORDER BY "LogID" DESC LIMIT 1',
        (MERGE_AUDIT_ACTION, loser_id),
    )
    audit_row = cur.fetchone()
    audit_log_id = audit_row[0] if audit_row else None

    # Mark the approval EXECUTED -- the transition Phase 4I explicitly left
    # for "a future executor" to implement (merge_approval.py's own module
    # docstring). Reuses is_legal_transition() rather than re-deciding
    # legality inline. Guarded a second time in the UPDATE's own WHERE
    # clause (Status = 'APPROVED') as defense-in-depth against this row
    # having somehow changed underneath the held lock.
    if not is_legal_transition(approval.status, ma.STATUS_EXECUTED):
        raise RuntimeError(
            f"unexpected: MergeApproval {approval.approval_id} status "
            f"{approval.status!r} cannot legally transition to EXECUTED -- aborting"
        )
    cur.execute(
        'UPDATE "MergeApproval" SET "Status" = %s, "ExecutedAt" = NOW(), '
        '"ExecutionAuditLogID" = %s WHERE "ApprovalID" = %s AND "Status" = %s',
        (ma.STATUS_EXECUTED, audit_log_id, approval.approval_id, ma.STATUS_APPROVED),
    )

    # Final invariant check, where feasible: survivor still present, loser
    # now gone. A cheap, direct confirmation on top of merge_group()'s own
    # internal profile-preservation assertion (which already ran, above).
    cur.execute('SELECT "PaperID" FROM "ResearchPaper" WHERE "PaperID" = %s', (survivor_id,))
    if cur.fetchone() is None:
        raise RuntimeError(
            f"post-merge invariant violated: survivor {survivor_id} is missing after merge_group() -- aborting"
        )
    cur.execute('SELECT "PaperID" FROM "ResearchPaper" WHERE "PaperID" = %s', (loser_id,))
    if cur.fetchone() is not None:
        raise RuntimeError(
            f"post-merge invariant violated: loser {loser_id} is still present after merge_group() -- aborting"
        )

    return ExecutionResult(ok=True, approval_id=approval.approval_id, audit_log_id=audit_log_id)
