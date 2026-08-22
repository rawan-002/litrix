"""Phase 4G -- READ-ONLY execution-safety primitives for a future duplicate-
ResearchPaper merge executor. Prototypes ONLY: content fingerprinting,
a locked-state preflight, an idempotency preflight, and an approval-artifact
schema. This module does NOT merge, DELETE, or execute anything.

Hard boundary, by design (see test_merge_execution_safety.py's static guard
tests for the automated proof):
  - No DELETE/UPDATE/INSERT SQL exists anywhere in this file.
  - merge_group() is never imported or called.
  - dedup_papers.py --apply is never invoked.
  - No network client exists.
  - There is no loop that executes a plan -- every function here answers
    "would this be safe to execute", never "execute it".

Reuses dedup_papers.py's and merge_plan_generator.py's existing pure
functions/DB helpers wherever possible (choose_keep, pair_confidence,
hard_exclusion_reason, journal_id_decision, author_content_conflicts,
fetch_paper_row, fetch_authors_rows, build_papers_dict_for_pure_functions)
-- neither file is modified by this phase.
"""
import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR / "tools"))

REPORT_DIR = BACKEND_DIR / "reports"


# ===========================================================================
# TASK A -- Content Fingerprint Primitive
# ===========================================================================
#
# Which ResearchPaper fields participate, and why (Phase 4F's stale-plan
# analysis, re-applied field by field against merge_plan_generator.py's own
# classify_field()/build_journal_state()/build_doi_state() decision surface):
#
#   INCLUDED -- every field classify_field() can return CONFLICT/HUMAN_REVIEW
#   for, plus DOI and JournalID (each governed by their own dedicated state
#   object but still load-bearing for survivor selection / execution safety),
#   plus a derived `citations` value (NOT the raw RawData_Log blob -- see
#   below).
#
#   EXCLUDED, with reasons proven by existing code, not assumed:
#     - SearchVector_En, SearchVector_Ar: merge_plan_generator.DERIVED_FIELDS
#       -- tsvector, regenerated from Title/Abstract, never diffed, never
#       drives any decision.
#     - ScrapedAt, VerifiedAt, DoiResolvedAt:
#       merge_plan_generator.PROCESS_TIMESTAMP_FIELDS -- classify_field()
#       always resolves these to KEEP_WINNER even when both sides differ;
#       they never produce CONFLICT/HUMAN_REVIEW and never affect
#       choose_keep(). Phase 4F separately proved ResearchPaper has no
#       generic "UpdatedAt" column at all.
#     - RawData_Log (the raw jsonb blob): superseded below by a derived
#       `citations` integer -- the ONLY thing choose_keep()'s citations
#       tiebreak and dashboard code actually read out of it
#       (COALESCE(RawData_Log->cited_by->value, RawData_Log->cited_by_count,
#       0), the same expression dedup_papers.py's load_papers() and
#       merge_plan_generator.py's build_papers_dict_for_pure_functions()
#       already use). The rest of the blob (source URLs, per-scrape
#       metadata) never enters any survivor-selection, field-decision, or
#       conflict-status computation, so including it would fail the task's
#       explicit "irrelevant presentation details should not create
#       accidental nondeterminism" requirement.
#
#   Authors rows (UserID, AuthorNameRaw) for BOTH sides are included even
#   though they are not ResearchPaper columns at all -- Phase 4E proved
#   AuthorNameRaw content drift for a shared UserID is exactly the silent
#   data-loss path this whole safety line of work exists to prevent, so it
#   is unambiguously "a field whose drift could change... conflict status".
FINGERPRINT_RP_FIELDS = [
    "JournalID", "Title", "Title_En", "Abstract", "Abstract_En", "Language",
    "DOI", "PubYear", "Volume", "Issue", "Pages", "IsVerified", "Source",
    "NormalizedTitle", "Indexing", "CitationsByYear", "TenantID",
    "AffiliationVerified", "VerificationSource", "VerificationDetails",
    "VenueType", "DoiResolvedBy", "OpenAlexWorkID", "AbstractSource",
    "PdfUrl", "PdfAccessType", "PublicationType",
]


def _looks_like_json_container(s):
    """Cheap, safe heuristic: does this string structurally look like a
    JSON object or array, rather than ordinary text? Checked before ever
    attempting json.loads() on a string field -- see _normalize_scalar()'s
    docstring (Phase 4P.1) for why this exists and why it is deliberately
    conservative. A real Title/Abstract/DOI essentially never starts with
    '{' or '[' and ends with the matching '}'/']', so this cannot mistake
    ordinary prose for JSON; it can only ever flag genuine
    object-or-array-shaped text for the one narrow case that matters."""
    t = s.strip()
    return (t.startswith("{") and t.endswith("}")) or (t.startswith("[") and t.endswith("]"))


def _normalize_scalar(value):
    """Deterministic normalization for one field value.

    - None and an all-whitespace/empty string normalize to the SAME value
      (None) -- matching merge_plan_generator.py's own _is_empty() equivalence,
      since a bare NULL<->'' transition never changes any classify_field()
      verdict, so it must not change the fingerprint either.
    - bool is checked before int (bool is an int subclass in Python).
    - int/float pass through as-is.
    - dict/list (jsonb columns: CitationsByYear, VerificationDetails) is
      serialized via json.dumps(sort_keys=True) so key-insertion-order
      never affects the result -- pure presentation detail, must not cause
      nondeterminism. sort_keys applies recursively to any dict nested
      inside a list too, while never reordering the list's own elements
      (JSON arrays are inherently ordered; sort_keys only affects object
      key order) -- list element order remains fingerprint-significant.
    - str is checked for the ONE real representation-equivalence gap this
      repository's two real database-connection mechanisms exhibit
      (Phase 4P/4P.1): a bare psycopg2.connect() connection (litrix_db.db())
      auto-decodes a jsonb column into a native dict; Django's own
      connection.cursor(), used for the exact same raw SQL, hands back the
      SAME column as an undecoded JSON string instead. Both are
      semantically identical data (proven live, Phase 4P.1 Task A) -- only
      the Python representation differs by which mechanism fetched it, and
      a fingerprint computed from one must equal a fingerprint computed
      from the other for identical underlying data. This is NOT a blanket
      "try to json.loads every string" rule -- _looks_like_json_container()
      gates it to strings that are structurally object/array-shaped first;
      an ordinary text field is returned completely unchanged, and a
      string that merely LOOKS like JSON but fails to parse (malformed)
      falls back to plain-string handling rather than raising.
    - anything else (e.g. a date/datetime that slipped through) is coerced
      to str() as a last resort -- none of the INCLUDED fields are
      timestamps, so this branch is not expected to fire in practice.
    """
    if value is None:
        return None
    if isinstance(value, str):
        if _looks_like_json_container(value):
            try:
                parsed = json.loads(value)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, (dict, list)):
                return _normalize_scalar(parsed)
        return value if value.strip() != "" else None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (dict, list)):
        if len(value) == 0:
            return None
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return str(value)


def _normalize_authors(authors):
    """authors: iterable of {"UserID":..., "AuthorNameRaw":...}. Sorted by
    UserID so list ORDER (a presentation detail -- SQL gives no ordering
    guarantee without ORDER BY) can never affect the fingerprint, only
    CONTENT can."""
    rows = [
        {"UserID": a["UserID"], "AuthorNameRaw": _normalize_scalar(a.get("AuthorNameRaw"))}
        for a in authors
    ]
    rows.sort(key=lambda a: a["UserID"])
    return rows


def compute_plan_fingerprint(winner_id, loser_id, winner_row, loser_row,
                              winner_authors, loser_authors, winner_citations, loser_citations):
    """Pure function. Deterministic SHA-256 fingerprint of exactly the
    source state a merge plan's field_actions/journal_state/
    author_content_conflicts were computed from.

    winner_row/loser_row: dicts as returned by
    merge_plan_generator.fetch_paper_row() (DOI already normalized the
    same way the rest of the pipeline normalizes it -- this function does
    not re-normalize DOI with different logic, it trusts the caller's
    already-established normalization, the same one choose_keep()/
    pair_confidence()/classify_field() all already operate on).
    winner_authors/loser_authors: as returned by
    merge_plan_generator.fetch_authors_rows().
    winner_citations/loser_citations: the derived citations integer (same
    COALESCE(...) expression dedup_papers.py's load_papers() computes) --
    passed in explicitly rather than computed here, keeping this function
    free of any DB/JSON-extraction logic.

    Same logical state -> same fingerprint, always (SHA-256 over a
    canonical, sort_keys=True JSON serialization -- no dict-ordering,
    list-ordering, or NULL-vs-empty-string nondeterminism is possible).
    Any load-bearing field's drift -> a different fingerprint (every field
    in FINGERPRINT_RP_FIELDS is included as-is, with no fuzzy tolerance).
    """
    payload = {
        "winner_id": winner_id,
        "loser_id": loser_id,
        "winner": {f: _normalize_scalar(winner_row.get(f)) for f in FINGERPRINT_RP_FIELDS},
        "loser": {f: _normalize_scalar(loser_row.get(f)) for f in FINGERPRINT_RP_FIELDS},
        "winner_citations": int(winner_citations or 0),
        "loser_citations": int(loser_citations or 0),
        "winner_authors": _normalize_authors(winner_authors),
        "loser_authors": _normalize_authors(loser_authors),
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ===========================================================================
# TASK B -- Locked-State Preflight Prototype
# ===========================================================================
#
# Repository-native locking precedent (re-confirmed this phase, same as
# Phase 4F): raw-cursor `SELECT ... FOR UPDATE`, inside a
# transaction.atomic() block, via connection.cursor() -- used today in
# backend/analytics/reconciliation_views.py (and 6 other files). This
# module reuses that exact style; it introduces no new locking primitive.
#
# Three explicitly separated stages, matching the task's required API
# boundary:
#   1. fetch_current_state()   -- plain SELECT, no lock.
#   2. lock_pair_rows()        -- SELECT ... FOR UPDATE only. Never writes.
#   3. validate_against_plan() -- pure, no DB access at all.
#
# What this module does NOT do: open the transaction itself, decide what to
# do after a successful preflight, or perform any write. See
# "what_a_future_executor_must_do_after_success" below and in the JSON
# report for the documented handoff.

PREFLIGHT_OK = "OK"
PREFLIGHT_MISSING_ROW = "MISSING_ROW"
PREFLIGHT_REVERSED = "WINNER_LOSER_REVERSED"
PREFLIGHT_STALE_FINGERPRINT = "STALE_FINGERPRINT"
PREFLIGHT_DUPLICATE_SAFETY_FAILED = "DUPLICATE_SAFETY_FAILED"
PREFLIGHT_DOI_SAFETY_FAILED = "DOI_SAFETY_FAILED"


@dataclass(frozen=True)
class PreflightResult:
    status: str
    checks: dict
    reason: Optional[str] = None

    @property
    def passed(self):
        return self.status == PREFLIGHT_OK


def fetch_current_state(cur, winner_id, loser_id):
    """Stage 1 -- plain SELECT, no lock. Returns (winner_row, loser_row,
    winner_authors, loser_authors, winner_citations, loser_citations) using
    merge_plan_generator.py's own existing, unmodified fetch helpers.
    Either row dict is None if that PaperID does not exist."""
    from merge_plan_generator import fetch_paper_row, fetch_authors_rows

    winner_row = fetch_paper_row(cur, winner_id)
    loser_row = fetch_paper_row(cur, loser_id)
    winner_authors = fetch_authors_rows(cur, winner_id) if winner_row else []
    loser_authors = fetch_authors_rows(cur, loser_id) if loser_row else []
    winner_citations = _fetch_citations(cur, winner_id) if winner_row else 0
    loser_citations = _fetch_citations(cur, loser_id) if loser_row else 0
    return winner_row, loser_row, winner_authors, loser_authors, winner_citations, loser_citations


def _fetch_citations(cur, pid):
    """Same COALESCE(...) expression dedup_papers.py's load_papers() and
    merge_plan_generator.py's build_papers_dict_for_pure_functions() already
    use -- SELECT only."""
    cur.execute('''SELECT COALESCE(("RawData_Log"->'cited_by'->>'value')::int,
                            ("RawData_Log"->>'cited_by_count')::int, 0)
                   FROM "ResearchPaper" WHERE "PaperID" = %s''', (pid,))
    row = cur.fetchone()
    return int(row[0] or 0) if row else 0


def reject_self_merge(winner_id, loser_id):
    """Pure, no DB access. A pair where winner_id == loser_id is a caller
    logic error, not a legitimate merge candidate under any circumstance --
    checked first, before any lock is attempted, so a self-merge never even
    reaches the database. Returns (ok, reason)."""
    if winner_id == loser_id:
        return False, f"winner_id and loser_id are both {winner_id} -- a PaperID cannot merge with itself"
    return True, None


# ===========================================================================
# Phase 4U -- Cross-tenant isolation guard.
#
# Investigated and confirmed (backend/reports/phase4u_cross_tenant_enforcement.md,
# Task A): no schema-level mechanism (FK, CHECK constraint, trigger, RLS
# policy, DB function) and no code-level check anywhere in
# merge_approval.py/merge_executor.py/merge_execution_safety.py/
# dedup_papers.py enforced that SurvivorPaperID and LoserPaperID belong to
# the same TenantID. merge_plan_generator.compute_classification()'s
# tenant_blocked rule exists but is a PLAN-GENERATION-time check that
# execute_approved_merge() never calls -- it protected nothing at the
# actual approval/execution boundary. This was a real, confirmed defect,
# not a merely-untested branch.
#
# validate_same_tenant() is deliberately pure (no DB access, matching
# reject_self_merge()'s own shape exactly) so it can be called from BOTH
# enforcement boundaries that actually matter -- approval creation
# (merge_approval.py::create_pending_approval(), which has no ResearchPaper
# row in hand yet and uses fetch_paper_tenant_ids() below to get one) and
# the executor immediately before any write (merge_executor.py::
# execute_approved_merge(), which already has winner_row/loser_row fetched
# via fetch_current_state() and needs no new query at all -- TenantID is
# already one of RP_COLUMNS). Two independent enforcement points, not one,
# per the explicit instruction not to assume one layer blocking the case
# makes a second layer unnecessary -- each protects a distinct entry path
# (a bad approval never gets created; and, independently, even an
# already-existing approval that somehow slipped through can never reach a
# write).
# ===========================================================================

def validate_same_tenant(survivor_tenant_id, loser_tenant_id):
    """Pure, no DB access. Both papers must belong to the same tenant -- a
    cross-tenant merge crosses a multi-tenant data-isolation boundary and
    must never be planned, approved, or executed automatically (matches
    merge_plan_generator.compute_classification()'s own tenant_blocked
    rule, applied here at the two points that actually write/authorize a
    write, not only the point that plans). A None on either side is
    reported as its own distinct reason, not silently treated as a
    successful match (None == None passing would be wrong -- it would mean
    two nonexistent/unresolvable papers "match"). Returns (ok, reason)."""
    if survivor_tenant_id is None or loser_tenant_id is None:
        return False, "one or both papers have no resolvable TenantID -- cannot verify tenant match"
    if survivor_tenant_id != loser_tenant_id:
        return False, (
            f"cross-tenant pair: survivor TenantID={survivor_tenant_id!r}, "
            f"loser TenantID={loser_tenant_id!r} -- a merge must never cross tenants"
        )
    return True, None


def fetch_paper_tenant_ids(cur, survivor_id, loser_id):
    """SELECT-only. Returns (survivor_tenant_id, loser_tenant_id) -- either
    is None if that PaperID does not currently exist. Used by
    create_pending_approval(), which -- unlike the executor -- has no
    ResearchPaper row already in hand at the point it needs to check this."""
    cur.execute(
        'SELECT "PaperID", "TenantID" FROM "ResearchPaper" WHERE "PaperID" = ANY(%s)',
        ([survivor_id, loser_id],),
    )
    by_id = {pid: tid for pid, tid in cur.fetchall()}
    return by_id.get(survivor_id), by_id.get(loser_id)


# ---------------------------------------------------------------------------
# Phase 4Y -- narrow case-only AuthorNameRaw classifier. Policy decision
# (see backend/reports/phase4y_case_only_author_policy.md): Option B,
# CASE_ONLY_ALLOWED_FOR_HUMAN_REVIEW_ONLY. This function is a pure
# CLASSIFICATION SIGNAL for plan/reason reporting only -- it never grants
# execution permission by itself, is never consulted by merge_executor.py
# or merge_approval.py, and does not change author_content_conflicts()'s
# own exact-equality blocking behavior in dedup_papers.py (untouched by
# this phase). A case-only conflict still blocks automatic execution
# exactly like every other AuthorNameRaw conflict; only its LABEL in the
# plan output changes, from an undifferentiated conflict to one explicitly
# marked as a deterministic formatting-only difference, for a human
# reviewer to weigh.
# ---------------------------------------------------------------------------

def is_case_only_difference(a, b):
    """Pure, no DB access, no network, no side effects. Deterministic
    classification of whether two AuthorNameRaw strings differ ONLY in
    letter case.

    Deliberately far narrower than disambiguation/pipeline.py's
    normalize_name() (Phase 4X's investigated, and rejected-for-this-
    purpose, general-purpose tool): the ONLY transformation applied here
    is str.lower(). No punctuation stripping, no whitespace collapsing,
    no diacritic/Unicode normalization, no fuzzy or substring matching,
    no initials expansion, no token reordering. Phase 4X directly proved
    normalize_name() collapses "Al-Amin"/"Al Amin" and "D'Angelo"/
    "D Angelo" -- both remain correctly distinct under this function,
    since punctuation is never touched.

    None and empty-string values are NEVER treated as equivalent to
    anything, including each other -- a missing value on either side (or
    both) always returns (False, ...), never silently reported as a
    case-only match. An identical pair (nothing to classify -- there is
    no difference to begin with) also returns (False, ...): this
    function answers "is this a case-only DIFFERENCE", not "could these
    ever be considered the same".

    Returns (is_case_only, reason), matching validate_same_tenant()'s
    own (ok, reason) convention.
    """
    if a is None or b is None:
        return False, "one or both values are missing (None) -- not comparable, not case-only"
    if a == "" or b == "":
        return False, "one or both values are empty strings -- not comparable, not case-only"
    if a == b:
        return False, "values are already identical -- there is no difference to classify"
    if len(a) != len(b):
        return False, "character counts differ -- not a pure case difference"
    if a.lower() != b.lower():
        return False, "values differ by more than letter case under lower() alone"
    return True, "values are identical after lower() alone, with no other transformation applied -- differ only in letter case"


def build_lock_order(id_a, id_b):
    """Pure, no DB access. Deterministic lock-acquisition order: always
    ascending by PaperID, independent of which side is 'winner'/'loser'.

    Why this matters even though both rows are locked by ONE SQL statement
    (SELECT ... WHERE PaperID = ANY(array) FOR UPDATE, which Postgres
    executes as a single atomic step -- there is no deadlock possible
    *within* one such statement): the risk this guards against is a FUTURE
    executor accidentally splitting this into two sequential
    SELECT ... FOR UPDATE statements (one per row), or two different
    executor invocations racing over an overlapping pair of PaperIDs. The
    standard, repository-agnostic defense against that class of deadlock is
    exactly this -- always acquire locks in one consistent, deterministic
    order (ascending ID here) regardless of each row's semantic role in the
    operation. Applying it now, at the query-building layer, means any
    future change to how the lock is issued inherits the safety property
    automatically rather than needing to remember to add it later."""
    return tuple(sorted((id_a, id_b)))


def lock_pair_rows(cur, winner_id, loser_id):
    """Stage 2 -- SELECT ... FOR UPDATE only. Never writes. Caller MUST
    already be inside a transaction (this function does not open or manage
    one -- see the documented handoff below). Raises ValueError immediately
    on a self-merge attempt (reject_self_merge()), before issuing any SQL.
    IDs are locked in deterministic ascending order (build_lock_order()).
    Returns True if both rows were successfully locked, False if either is
    missing (already deleted by a prior/concurrent merge)."""
    ok, reason = reject_self_merge(winner_id, loser_id)
    if not ok:
        raise ValueError(reason)

    ordered_ids = list(build_lock_order(winner_id, loser_id))
    cur.execute(
        'SELECT "PaperID" FROM "ResearchPaper" WHERE "PaperID" = ANY(%s) FOR UPDATE',
        (ordered_ids,),
    )
    locked = {r[0] for r in cur.fetchall()}
    return winner_id in locked and loser_id in locked


def validate_against_plan(plan, winner_row, loser_row, winner_authors, loser_authors,
                           winner_citations, loser_citations, pair_confidence_val, hard_excl,
                           doi_claimed_elsewhere):
    """Stage 3 -- pure, no DB access. `plan` is the previously-generated
    plan dict (merge_plan_generator.py's shape, extended with a
    'plan_fingerprint' field this phase introduces). Everything else is
    already-fetched, already-computed data the caller supplies."""
    checks = {}

    if winner_row is None or loser_row is None:
        return PreflightResult(PREFLIGHT_MISSING_ROW, checks,
                                "one or both PaperIDs no longer exist in ResearchPaper")

    checks["both_rows_exist"] = True

    reversed_ = (plan["survivor"] != winner_row["PaperID"]) or (plan["loser"] != loser_row["PaperID"])
    checks["winner_loser_unreversed"] = not reversed_
    if reversed_:
        return PreflightResult(PREFLIGHT_REVERSED, checks,
                                f"plan recorded survivor={plan['survivor']}/loser={plan['loser']}, "
                                f"but current call is winner={winner_row['PaperID']}/loser={loser_row['PaperID']}")

    current_fp = compute_plan_fingerprint(
        winner_row["PaperID"], loser_row["PaperID"], winner_row, loser_row,
        winner_authors, loser_authors, winner_citations, loser_citations)
    fp_match = current_fp == plan.get("plan_fingerprint")
    checks["fingerprint_match"] = fp_match
    checks["current_fingerprint"] = current_fp
    checks["plan_fingerprint"] = plan.get("plan_fingerprint")
    if not fp_match:
        return PreflightResult(PREFLIGHT_STALE_FINGERPRINT, checks,
                                "live data no longer matches the fingerprint recorded on the approved plan")

    checks["pair_confidence"] = pair_confidence_val
    checks["hard_exclusion_reason"] = hard_excl
    if pair_confidence_val != "high" or hard_excl:
        return PreflightResult(PREFLIGHT_DUPLICATE_SAFETY_FAILED, checks,
                                f"pair_confidence={pair_confidence_val!r}, hard_exclusion_reason={hard_excl!r}")

    checks["doi_claimed_elsewhere"] = doi_claimed_elsewhere
    if doi_claimed_elsewhere:
        return PreflightResult(PREFLIGHT_DOI_SAFETY_FAILED, checks,
                                "winner's DOI is now claimed by a different ResearchPaper row than when the plan was built")

    return PreflightResult(PREFLIGHT_OK, checks, None)


def is_doi_claimed_elsewhere(cur, doi, exclude_paper_ids):
    """SELECT-only DOI-uniqueness re-check, same normalized-DOI comparison
    uq_paper_doi_normalized already enforces at the DB level -- this is a
    live re-verification, not a new uniqueness rule. Returns True if some
    OTHER ResearchPaper row (not in exclude_paper_ids) now carries this
    DOI. No-op (False) if doi is empty."""
    if not doi:
        return False
    cur.execute(
        'SELECT "PaperID" FROM "ResearchPaper" WHERE LOWER("DOI") = LOWER(%s) AND "PaperID" != ALL(%s)',
        (doi, list(exclude_paper_ids)),
    )
    return cur.fetchone() is not None


WHAT_A_FUTURE_EXECUTOR_MUST_DO_AFTER_PREFLIGHT_SUCCESS = """
This preflight only ANSWERS "is it currently safe to proceed" -- it does not
itself proceed. A future executor that receives PreflightResult.passed==True
while still holding the SELECT ... FOR UPDATE lock (the same transaction,
uncommitted) must, in order, still inside that same transaction:
  1. Apply the plan's deterministic field decisions (JournalID backfill
     etc.) -- see Phase 4F's transaction_steps design.
  2. Run merge_group()'s existing, unmodified child-migration logic.
  3. Write the AuditLog row (before deleting the loser, as merge_group()
     already does).
  4. DELETE the loser ResearchPaper row.
  5. Re-run the existing profile-preservation assertion.
  6. COMMIT -- which also releases the FOR UPDATE lock.
If ANY of steps 1-5 fails, the transaction must roll back in full (the lock
is released by the rollback), leaving the database exactly as this
preflight found it. This module implements none of steps 1-6 -- doing so
would be building the executor itself, explicitly out of scope this phase.
"""


# ===========================================================================
# TASK C -- Idempotency Preflight
# ===========================================================================
#
# AuditLog schema (re-confirmed read-only this phase): LogID (PK,
# autoincrement), TenantID, UserID, Action, TargetType, TargetID, Metadata
# (jsonb), IpAddress, UserAgent, CreatedAt. merge_group() already writes one
# row per merged loser: Action='paper.merge.dedup', TargetType='ResearchPaper',
# TargetID=<loser PaperID>, Metadata={"kept_paper_id": <winner>, ...}.
# 59 such rows exist in the live database today from real historical
# --apply runs (Phase 4F finding, re-confirmed below).
#
# AuditLog has NO unique constraint on (Action, TargetID) -- LogID is a bare
# autoincrement PK. It is therefore an EVIDENCE SOURCE for idempotency, not
# a database-enforced guarantee; this preflight is what turns that evidence
# into a safe decision.

IDEMPOTENCY_ELIGIBLE = "ELIGIBLE"
IDEMPOTENCY_BLOCKED_EXACT_PRIOR_EXECUTION = "BLOCKED_EXACT_PRIOR_EXECUTION"
IDEMPOTENCY_BLOCKED_REVERSED_PRIOR_PAIR = "BLOCKED_REVERSED_PRIOR_PAIR"
IDEMPOTENCY_BLOCKED_CONTRADICTORY_HISTORY = "BLOCKED_CONTRADICTORY_HISTORY"
IDEMPOTENCY_UNKNOWN_HISTORY_BLOCKED = "UNKNOWN_HISTORY_BLOCKED"

MERGE_AUDIT_ACTION = "paper.merge.dedup"


@dataclass(frozen=True)
class IdempotencyResult:
    status: str
    reason: str
    evidence: list = field(default_factory=list)

    @property
    def eligible(self):
        return self.status == IDEMPOTENCY_ELIGIBLE


def fetch_merge_audit_rows(cur, paper_ids):
    """SELECT-only. Returns AuditLog rows (as dicts) for
    Action='paper.merge.dedup' where TargetID is one of paper_ids."""
    cur.execute(
        'SELECT "LogID","TargetID","Metadata","CreatedAt" FROM "AuditLog" '
        'WHERE "Action" = %s AND "TargetID" = ANY(%s) ORDER BY "LogID"',
        (MERGE_AUDIT_ACTION, list(paper_ids)),
    )
    rows = []
    for log_id, target_id, metadata, created_at in cur.fetchall():
        rows.append({"LogID": log_id, "TargetID": target_id, "Metadata": metadata, "CreatedAt": created_at})
    return rows


def check_idempotency(audit_rows, winner_id, loser_id):
    """Pure function -- audit_rows: list of {"LogID","TargetID","Metadata","CreatedAt"}
    dicts, already scoped to Action='paper.merge.dedup' and
    TargetID in {winner_id, loser_id} (see fetch_merge_audit_rows()).
    Unrelated AuditLog rows (other PaperIDs, other Actions) must never even
    reach this function -- the fetch already excludes them, so this
    function cannot falsely block on them by construction.

    Returns an IdempotencyResult. Never guesses: any row whose Metadata
    doesn't unambiguously answer "which pair was this" blocks with
    UNKNOWN_HISTORY_BLOCKED rather than being silently ignored or assumed
    safe.
    """
    if not audit_rows:
        return IdempotencyResult(IDEMPOTENCY_ELIGIBLE, "no prior paper.merge.dedup history for either PaperID", [])

    for row in audit_rows:
        meta = row.get("Metadata")
        if not isinstance(meta, dict) or "kept_paper_id" not in meta:
            return IdempotencyResult(
                IDEMPOTENCY_UNKNOWN_HISTORY_BLOCKED,
                f"AuditLog row LogID={row['LogID']} (TargetID={row['TargetID']}) has missing/malformed "
                f"Metadata -- cannot determine which merge this was, refusing to guess",
                [row],
            )

    exact = [r for r in audit_rows if r["TargetID"] == loser_id and r["Metadata"]["kept_paper_id"] == winner_id]
    if exact:
        return IdempotencyResult(
            IDEMPOTENCY_BLOCKED_EXACT_PRIOR_EXECUTION,
            f"loser {loser_id} was already merged into winner {winner_id} (AuditLog LogID={exact[0]['LogID']})",
            exact,
        )

    reversed_ = [r for r in audit_rows if r["TargetID"] == winner_id and r["Metadata"]["kept_paper_id"] == loser_id]
    if reversed_:
        return IdempotencyResult(
            IDEMPOTENCY_BLOCKED_REVERSED_PRIOR_PAIR,
            f"the current winner {winner_id} was already merged AWAY into the current loser {loser_id} "
            f"in the opposite direction historically (AuditLog LogID={reversed_[0]['LogID']}) -- "
            f"executing the current plan would contradict recorded history",
            reversed_,
        )

    # Either ID appears in merge history, but pointing somewhere else entirely.
    contradictory = [r for r in audit_rows
                      if (r["TargetID"] == loser_id and r["Metadata"]["kept_paper_id"] != winner_id)
                      or (r["TargetID"] == winner_id and r["Metadata"]["kept_paper_id"] != loser_id)]
    if contradictory:
        return IdempotencyResult(
            IDEMPOTENCY_BLOCKED_CONTRADICTORY_HISTORY,
            f"one of {winner_id}/{loser_id} already has completed-merge history involving a THIRD PaperID "
            f"(AuditLog LogID={contradictory[0]['LogID']}) -- this contradicts treating {winner_id}/{loser_id} "
            f"as a fresh, independent pair",
            contradictory,
        )

    # Should be unreachable given the three cases above are exhaustive for
    # any row with a well-formed Metadata['kept_paper_id'] and
    # TargetID in {winner_id, loser_id} -- kept as a defensive default
    # rather than assumed safe.
    return IdempotencyResult(
        IDEMPOTENCY_UNKNOWN_HISTORY_BLOCKED,
        "audit history exists for this pair but did not match any recognized pattern -- refusing to guess",
        audit_rows,
    )


# --- Coarse 3-way verdict, matching this phase's requested contract -------
#
# EXISTING_AUDITLOG_SUFFICIENT vs EXISTING_AUDITLOG_INSUFFICIENT vs
# NEW_IDEMPOTENCY_MECHANISM_REQUIRED (the "is AuditLog good enough at all"
# question) is answered once, in prose, in the accompanying report -- the
# verdict is EXISTING_AUDITLOG_SUFFICIENT precisely because check_idempotency()
# above, built entirely from AuditLog evidence, already correctly answers
# every required scenario (proven by the 7 tests it already has, plus the 3
# new ones this phase adds for the terms below). No new storage was needed
# to answer THIS question -- only a query and a decision function over
# what already exists and is already populated (59 real rows).
#
# idempotency_verdict() is the specific 3-value return contract this
# phase's task text asks for (NOT_PREVIOUSLY_EXECUTED / ALREADY_EXECUTED /
# HISTORICAL_STATE_AMBIGUOUS) -- a coarser view over the same, already-built
# check_idempotency() logic, not a re-implementation of it.

IDEMPOTENCY_NOT_PREVIOUSLY_EXECUTED = "NOT_PREVIOUSLY_EXECUTED"
IDEMPOTENCY_ALREADY_EXECUTED = "ALREADY_EXECUTED"
IDEMPOTENCY_HISTORICAL_STATE_AMBIGUOUS = "HISTORICAL_STATE_AMBIGUOUS"

AUDITLOG_SUFFICIENCY_VERDICT = "EXISTING_AUDITLOG_SUFFICIENT"
AUDITLOG_SUFFICIENCY_REASONING = (
    "AuditLog already carries everything the three required idempotency "
    "verdicts need: Action='paper.merge.dedup' scopes to merge events "
    "specifically; TargetID identifies the merged-away loser; "
    "Metadata['kept_paper_id'] identifies the survivor -- together these "
    "fully determine 'was this exact pair, or its reverse, or a "
    "contradictory third-party outcome, already recorded'. The only real "
    "gap is that a malformed/missing Metadata row can't be resolved from "
    "AuditLog alone -- which is exactly why HISTORICAL_STATE_AMBIGUOUS "
    "exists as its own verdict rather than being silently treated as "
    "either eligible or blocked."
)


def idempotency_verdict(audit_rows, winner_id, loser_id):
    """Pure function. Returns (verdict, detailed_result) where verdict is
    one of NOT_PREVIOUSLY_EXECUTED / ALREADY_EXECUTED /
    HISTORICAL_STATE_AMBIGUOUS, and detailed_result is the finer-grained
    IdempotencyResult from check_idempotency() (preserving the exact-vs-
    reversed-vs-contradictory distinction for anyone who needs it, rather
    than discarding that evidence just to fit the 3-value contract)."""
    detailed = check_idempotency(audit_rows, winner_id, loser_id)
    if detailed.status == IDEMPOTENCY_ELIGIBLE:
        return IDEMPOTENCY_NOT_PREVIOUSLY_EXECUTED, detailed
    if detailed.status == IDEMPOTENCY_UNKNOWN_HISTORY_BLOCKED:
        return IDEMPOTENCY_HISTORICAL_STATE_AMBIGUOUS, detailed
    # BLOCKED_EXACT_PRIOR_EXECUTION / BLOCKED_REVERSED_PRIOR_PAIR /
    # BLOCKED_CONTRADICTORY_HISTORY all mean "history already has a
    # completed, recorded outcome for this identity" -- ALREADY_EXECUTED.
    return IDEMPOTENCY_ALREADY_EXECUTED, detailed


# ===========================================================================
# TASK D -- Approval State Design
# ===========================================================================
#
# CLASSIFICATION: B) No legitimate existing approval storage exists.
#
# AuditLog was evaluated and rejected as approval storage (not because it
# is unrelated -- it is the CLOSEST candidate in the schema, already used by
# the same subsystem -- but because it fails the task's "exact purpose
# proven" bar):
#   - AuditLog is a pure APPEND-ONLY action log (no UPDATE path exists
#     anywhere in this codebase for a AuditLog row once inserted). Approval
#     is inherently MUTABLE STATE with a current status (PENDING ->
#     APPROVED/REJECTED) that must be queryable as a single current answer.
#     Modeling "current approval status" on an append-only log requires
#     ad-hoc "most recent row wins" logic layered on top -- exactly the
#     kind of invented, unproven semantic the task instructs against.
#   - AuditLog has no typed column for approval status, plan fingerprint,
#     or an immutable approval version -- only its schema-free `Metadata`
#     jsonb could hold them, which is precisely "a table that can store
#     text" rather than structured, purpose-built storage. The task
#     explicitly names this exact anti-pattern as one to avoid.
#   - Task C already uses AuditLog for idempotency history. Overloading the
#     SAME table for approval-state as well would make "is this an approval
#     record" vs "is this a completed-merge record" an unenforced
#     convention rather than a schema guarantee, adding real query-
#     ambiguity risk to a safety-critical check.
#
# No DB table is created this phase. Below is a minimal, pure-Python
# approval ARTIFACT schema -- suitable for a later Django migration
# (a real table) OR a file-based workflow (e.g. one JSON file per approval,
# git- or otherwise version-controlled) -- deliberately not choosing between
# those two for a future phase to decide, since nothing in this phase's
# evidence favors one over the other.

APPROVAL_PENDING = "PENDING"
APPROVAL_APPROVED = "APPROVED"
APPROVAL_REJECTED = "REJECTED"


@dataclass(frozen=True)
class ApprovalArtifact:
    plan_id: str
    winner_paper_id: int
    loser_paper_id: int
    plan_fingerprint: str
    approval_status: str
    approver_identity: Optional[str]
    approval_timestamp: Optional[str]  # ISO-8601 string, set only when status != PENDING
    approval_version: int  # starts at 1; a fingerprint change requires a NEW artifact (new plan_id or bumped version), never an in-place mutation of an existing one
    reason_notes: Optional[str] = None

    def to_dict(self):
        return asdict(self)


def validate_approval_artifact(artifact, current_plan_fingerprint):
    """Pure function. An approval is usable to authorize execution only if
    it is APPROVED and bound to the EXACT fingerprint currently in force --
    an approval recorded against an older plan must never authorize a
    changed one. Returns (is_valid, reason)."""
    if artifact is None:
        return False, "no approval artifact exists for this plan"
    if artifact.approval_status != APPROVAL_APPROVED:
        return False, f"approval_status is {artifact.approval_status!r}, not APPROVED"
    if artifact.plan_fingerprint != current_plan_fingerprint:
        return False, ("approval is bound to a different plan_fingerprint than the current plan "
                        "-- the underlying data changed since approval was granted, this approval "
                        "does not authorize the current state")
    return True, None


# --- 3-way approval-storage verdict, matching this phase's requested
# contract: EXISTING_APPROVAL_MECHANISM_USABLE /
# EXISTING_MECHANISM_REQUIRES_EXTENSION / NEW_APPROVAL_STORAGE_REQUIRED.
#
# Every candidate the task explicitly asks to check was investigated
# read-only against live schema this phase (see the accompanying report for
# the full evidence): AuditLog, ReportPaperDecision, AuthorReviewQueue.
# No other table in the schema matched '%approv%'/'%review%'/'%decision%'/
# '%queue%' (fresh information_schema.tables scan this phase).

APPROVAL_VERDICT_EXISTING_USABLE = "EXISTING_APPROVAL_MECHANISM_USABLE"
APPROVAL_VERDICT_REQUIRES_EXTENSION = "EXISTING_MECHANISM_REQUIRES_EXTENSION"
APPROVAL_VERDICT_NEW_STORAGE_REQUIRED = "NEW_APPROVAL_STORAGE_REQUIRED"


def approval_storage_verdict():
    """Pure, no DB access (the evidence behind this was already gathered
    read-only, see the report) -- returns (verdict, evidence) for the three
    tables investigated as approval-storage candidates.

    Verdict: NEW_APPROVAL_STORAGE_REQUIRED. None of the three real,
    existing candidates can safely represent 'pair X/Y was explicitly
    approved for merge under plan fingerprint Z' without either becoming a
    fundamentally different mechanism (AuditLog: append-only log, no
    current-status concept, no typed fingerprint/status/version columns --
    extending it would mean building approval-state semantics on top of an
    audit trail, not merely adding columns) or repurposing a table built
    for a genuinely different domain (ReportPaperDecision: single-PaperID
    report-correction workflow, no pair/fingerprint concept;
    AuthorReviewQueue: single-PaperID author-name-mapping suggestions, no
    pair/fingerprint concept, 0 rows in production). No schema change was
    made to reach this verdict, and none is proposed as part of it -- see
    ApprovalArtifact above for the artifact SHAPE this verdict implies is
    needed, deliberately left unbound to any specific storage mechanism.
    """
    evidence = {
        "AuditLog": (
            "Investigated in depth (schema + 59 real historical rows, re-confirmed this phase). "
            "REJECTED: append-only action log with no UPDATE path anywhere in this codebase; approval "
            "needs one queryable CURRENT status (PENDING->APPROVED/REJECTED), which an append-only log "
            "can only support via an invented 'most recent row wins' convention -- exactly what this task "
            "instructs against. No typed column for approval_status/plan_fingerprint/approval_version "
            "exists; only the schema-free Metadata jsonb could hold them."
        ),
        "ReportPaperDecision": (
            "Investigated this phase (44 real rows). REJECTED: real Decision+DecidedAt shape, but keyed "
            "to a single PaperID plus a SubmissionID (a report-correction workflow, semantically unrelated "
            "to duplicate-merge approval) -- no winner/loser pair concept, no fingerprint concept, wrong "
            "domain entirely, not merely missing a column."
        ),
        "AuthorReviewQueue": (
            "Investigated this phase (0 rows in production). REJECTED as approval storage: keyed to a "
            "single PaperID plus an author-name-mapping suggestion, no pair/fingerprint concept. Notable "
            "as the CLOSEST structural analog in the whole schema -- Status/ReviewedByUserID/ReviewedAt/"
            "ReviewerNotes is exactly a 'reviewer approves/rejects with notes' shape -- which independently "
            "confirms (rather than invents) the field shape already chosen for ApprovalArtifact above."
        ),
        "schema_sweep": (
            "Fresh information_schema.tables scan this phase for table names matching "
            "%approv%/%review%/%decision%/%queue% found exactly these two tables plus AuditLog -- no "
            "fourth candidate exists."
        ),
    }
    return APPROVAL_VERDICT_NEW_STORAGE_REQUIRED, evidence


# ===========================================================================
# TASK E -- Canary Integration Simulation (orchestration only, read-only)
# ===========================================================================

SIM_READY = "SIMULATION_READY"
SIM_BLOCKED_STALE_PLAN = "BLOCKED_STALE_PLAN"
SIM_BLOCKED_IDEMPOTENCY = "BLOCKED_IDEMPOTENCY"
SIM_BLOCKED_APPROVAL = "BLOCKED_APPROVAL"
SIM_BLOCKED_SAFETY_CONFLICT = "BLOCKED_SAFETY_CONFLICT"
SIM_UNKNOWN_BLOCKED = "UNKNOWN_BLOCKED"


def run_canary_simulation(cur, winner_id, loser_id, reference_plan, approval_artifact=None):
    """Read-only end-to-end simulation. Never writes. `reference_plan` must
    carry a 'plan_fingerprint' key (the fingerprint recorded when the plan
    was approved/generated) to compare against freshly-computed live state.
    `approval_artifact` is an ApprovalArtifact or None (Task D: none exists
    for any real pair today)."""
    from dedup_papers import pair_confidence, hard_exclusion_reason, choose_keep, norm_title

    steps = {}

    winner_row, loser_row, winner_authors, loser_authors, w_cit, l_cit = fetch_current_state(cur, winner_id, loser_id)
    steps["load_current_pair"] = {
        "winner_exists": winner_row is not None, "loser_exists": loser_row is not None,
    }
    if winner_row is None or loser_row is None:
        return {"final_verdict": SIM_UNKNOWN_BLOCKED, "reason": "one or both PaperIDs no longer exist", "steps": steps}

    current_fp = compute_plan_fingerprint(winner_id, loser_id, winner_row, loser_row,
                                           winner_authors, loser_authors, w_cit, l_cit)
    fp_match = current_fp == reference_plan.get("plan_fingerprint")
    steps["fingerprint_check"] = {"current": current_fp, "reference": reference_plan.get("plan_fingerprint"), "match": fp_match}
    if not fp_match:
        return {"final_verdict": SIM_BLOCKED_STALE_PLAN, "reason": "fingerprint mismatch against reference plan", "steps": steps}

    papers = {
        winner_id: {"paper_id": winner_id, "title": winner_row["Title"] or "", "doi": winner_row["DOI"],
                    "pub_year": winner_row["PubYear"], "is_verified": bool(winner_row["IsVerified"]),
                    "source": winner_row["Source"], "citations": w_cit,
                    "fuzzy_key": norm_title(winner_row["Title"]),
                    "authors": frozenset(a["UserID"] for a in winner_authors),
                    "first_author": winner_authors[0]["UserID"] if winner_authors else None},
        loser_id: {"paper_id": loser_id, "title": loser_row["Title"] or "", "doi": loser_row["DOI"],
                   "pub_year": loser_row["PubYear"], "is_verified": bool(loser_row["IsVerified"]),
                   "source": loser_row["Source"], "citations": l_cit,
                   "fuzzy_key": norm_title(loser_row["Title"]),
                   "authors": frozenset(a["UserID"] for a in loser_authors),
                   "first_author": loser_authors[0]["UserID"] if loser_authors else None},
    }
    keep, losers = choose_keep([winner_id, loser_id], papers)
    reversed_ = keep != winner_id
    pc = pair_confidence(winner_id, loser_id, papers)
    hard = hard_exclusion_reason(winner_id, loser_id, papers)
    steps["duplicate_safety_check"] = {"choose_keep_result": keep, "reversed": reversed_,
                                        "pair_confidence": pc, "hard_exclusion_reason": hard}
    if reversed_ or pc != "high" or hard:
        return {"final_verdict": SIM_BLOCKED_SAFETY_CONFLICT,
                "reason": f"reversed={reversed_}, pair_confidence={pc!r}, hard_exclusion_reason={hard!r}",
                "steps": steps}

    doi_conflict = is_doi_claimed_elsewhere(cur, winner_row["DOI"], [winner_id, loser_id])
    steps["doi_safety_check"] = {"winner_doi": winner_row["DOI"], "claimed_elsewhere": doi_conflict}
    if doi_conflict:
        return {"final_verdict": SIM_BLOCKED_SAFETY_CONFLICT, "reason": "winner DOI claimed by another row", "steps": steps}

    audit_rows = fetch_merge_audit_rows(cur, [winner_id, loser_id])
    idem = check_idempotency(audit_rows, winner_id, loser_id)
    steps["idempotency_check"] = {"status": idem.status, "reason": idem.reason}
    if not idem.eligible:
        return {"final_verdict": SIM_BLOCKED_IDEMPOTENCY, "reason": idem.reason, "steps": steps}

    approval_valid, approval_reason = validate_approval_artifact(approval_artifact, current_fp)
    steps["approval_check"] = {"artifact_present": approval_artifact is not None,
                                "valid": approval_valid, "reason": approval_reason}
    if not approval_valid:
        return {"final_verdict": SIM_BLOCKED_APPROVAL, "reason": approval_reason, "steps": steps}

    return {"final_verdict": SIM_READY, "reason": "all preflight checks passed", "steps": steps}
