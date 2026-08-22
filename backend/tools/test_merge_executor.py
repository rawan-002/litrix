"""Tests for merge_executor.py -- Phase 4J. Every scenario runs against
ExecutorFakeCursor, an in-memory double covering every SQL shape the
executor (and everything it reuses -- lock_pair_rows, validate_against_plan,
merge_group, merge_approval's transition machinery) can issue. No real
psycopg2/Django connection is opened anywhere in this file. No network call
is made. No production database is touched.

Run: cd backend && python tools/test_merge_executor.py
"""
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import merge_approval as ma  # noqa: E402
import merge_executor as mx  # noqa: E402
from merge_executor import (  # noqa: E402
    DEP_ACTION_BLOCK,
    DEP_ACTION_REMAP,
    DEPENDENCY_ACTION_MATRIX,
    EXEC_BLOCKED_ALREADY_EXECUTED,
    EXEC_BLOCKED_APPROVAL_NOT_APPROVED,
    EXEC_BLOCKED_APPROVAL_REVERSED,
    EXEC_BLOCKED_AUTHOR_CONFLICT,
    EXEC_BLOCKED_DEPENDENCY_GAP,
    EXEC_BLOCKED_HISTORY_AMBIGUOUS,
    EXEC_BLOCKED_JOURNAL_CONFLICT,
    EXEC_BLOCKED_MISSING_ROW,
    EXEC_BLOCKED_NO_APPROVAL,
    EXEC_BLOCKED_PREFLIGHT_FAILED,
    EXEC_BLOCKED_SELF_MERGE,
    ExecutionResult,
    check_unhandled_dependency_gaps,
    execute_approved_merge,
)
from merge_execution_safety import compute_plan_fingerprint, is_case_only_difference
from merge_plan_generator import (
    RP_COLUMNS,
    advance_case_only_review,
    REVIEW_STATE_HUMAN_REVIEW_REQUIRED,
    REVIEW_STATE_REVIEWED_APPROVED,
)


WRITE_VERBS = ("INSERT ", "UPDATE ", "DELETE ", "TRUNCATE ", "ALTER ", "DROP ", "CREATE ")


def _has_write(executed_sql):
    return any(any(v in sql.upper() for v in WRITE_VERBS) for sql in executed_sql)


def make_user(user_id=1, tenant_id=1, is_admin=True, perms=(), authenticated=True):
    perm_set = set(perms)
    return SimpleNamespace(
        user_id=user_id, tenant_id=tenant_id,
        user_type='Admin' if is_admin else 'Researcher',
        is_authenticated=authenticated,
        has_litrix_perm=lambda code: code in perm_set,
    )


def rp_row(**overrides):
    row = {c: None for c in RP_COLUMNS}
    row["PaperID"] = overrides.pop("PaperID", 1)
    row.update(overrides)
    return row


# ===========================================================================
# ExecutorFakeCursor -- one stateful double covering every SQL shape
# execute_approved_merge (and everything it reuses) can issue.
# ===========================================================================

class SimulatedForeignKeyViolation(Exception):
    """Stands in for psycopg2.errors.ForeignKeyViolation. Raised only when
    ExecutorFakeCursor is constructed with enforce_loser_paper_id_fk=True --
    a Python-level simulation of PostgreSQL's real, documented, undeferred
    ON DELETE NO ACTION semantics (a DELETE against a row referenced by such
    a constraint fails immediately if a referencing row exists and is not
    itself being deleted/updated in the same statement). This is NOT a real
    PostgreSQL instance -- none was safely available this phase (Phase
    4K.1) -- see test_fk_lifecycle.py's module docstring for the honest
    statement of what this can and cannot prove."""


class ExecutorFakeCursor:
    def __init__(self, research_papers=None, authors=None, audit_rows=None,
                 approvals=None, existing_tables=None, authors_column_list=None,
                 dependency_counts=None, inject_failure_on=(),
                 enforce_loser_paper_id_fk=False):
        self.research_papers = research_papers or {}   # PaperID -> dict(RP_COLUMNS + PaperID + _citations)
        self.authors = list(authors or [])              # [{"PaperID","UserID","AuthorNameRaw","AuthorOrder"}]
        self.audit_log = list(audit_rows or [])          # [(LogID, TargetID, Metadata(dict), CreatedAt)]
        self._next_audit_id = max([r[0] for r in self.audit_log], default=0) + 1
        self.approvals = approvals or {}                 # ApprovalID -> dict(APPROVAL_COLUMNS)
        self._next_approval_id = max(list(self.approvals.keys()) or [0]) + 1
        self.existing_tables = set(existing_tables or ())
        self.authors_column_list = list(authors_column_list or
                                         ["AuthorLinkID", "PaperID", "UserID", "AuthorNameRaw", "AuthorOrder"])
        self.dependency_counts = dict(dependency_counts or {})   # (table, PaperID) -> int
        self.inject_failure_on = tuple(inject_failure_on)
        # Simulates the ORIGINAL (Phase 4H/4I/4J) MergeApproval.LoserPaperID
        # schema -- ON DELETE NO ACTION -- when True. Default False matches
        # the CORRECTED (Phase 4K.1) schema -- no FK on LoserPaperID at all
        # -- which is what every pre-existing test in this file (and every
        # real, current migration file) now reflects.
        self.enforce_loser_paper_id_fk = enforce_loser_paper_id_fk
        self._result = None
        self.executed_sql = []

    # -- helpers -----------------------------------------------------------
    def _norm(self, sql):
        return " ".join(sql.split()).upper()

    def execute(self, sql, params=None):
        sql_upper = sql.upper()
        for marker in self.inject_failure_on:
            if marker in sql_upper:
                raise RuntimeError(f"injected failure at marker {marker!r}")
        self.executed_sql.append(sql)
        params = params or ()
        norm = self._norm(sql)

        # --- Phase 4U: fetch_paper_tenant_ids() (create_pending_approval(),
        # called during test fixture setup, not by execute_approved_merge()
        # itself -- that function reuses TenantID already present on the
        # rows this cursor's research_papers dict already returns). ---
        if sql_upper.startswith('SELECT "PAPERID", "TENANTID" FROM "RESEARCHPAPER"'):
            (ids,) = params
            self._result = [(pid, self.research_papers[pid].get("TenantID"))
                             for pid in ids if pid in self.research_papers]
            return

        # --- MergeApproval: row lock (approve/reject/revoke's _transition) ---
        if 'FROM "MERGEAPPROVAL"' in sql_upper and "FOR UPDATE" in sql_upper:
            (approval_id,) = params
            row = self.approvals.get(approval_id)
            self._result = [tuple(row[c] for c in ma.APPROVAL_COLUMNS)] if row else []
            return

        # --- MergeApproval: UPDATE (approve/reject/revoke OR our EXECUTED update) ---
        if sql_upper.startswith('UPDATE "MERGEAPPROVAL"'):
            # Our own EXECUTED update has no RETURNING clause; merge_approval.py's
            # _transition() always does -- and its RETURNING column list itself
            # contains the literal substring "ExecutionAuditLogID", so "RETURNING"
            # absence is the only reliable discriminator between the two shapes.
            if "EXECUTIONAUDITLOGID" in sql_upper and "RETURNING" not in sql_upper:
                status, audit_log_id, approval_id, expected_status = params
                row = self.approvals.get(approval_id)
                if row is not None and row["Status"] == expected_status:
                    row["Status"] = status
                    row["ExecutedAt"] = datetime.now(timezone.utc)
                    row["ExecutionAuditLogID"] = audit_log_id
                self._result = []
                return
            *set_params, approval_id = params
            row = self.approvals[approval_id]
            if '"REVIEWEDBYUSERID"' in sql_upper:
                status, reviewer, notes = set_params
                row["Status"], row["ReviewedByUserID"], row["ReviewerNotes"] = status, reviewer, notes
                row["ReviewedAt"] = datetime.now(timezone.utc)
            elif '"REVOKEDBYUSERID"' in sql_upper:
                status, revoker, reason = set_params
                row["Status"], row["RevokedByUserID"], row["RevocationReason"] = status, revoker, reason
                row["RevokedAt"] = datetime.now(timezone.utc)
            else:
                raise AssertionError(f"unrecognized MergeApproval UPDATE shape: {sql!r}")
            self._result = [tuple(row[c] for c in ma.APPROVAL_COLUMNS)]
            return

        # --- MergeApproval: INSERT (create_pending_approval) ---
        if sql_upper.startswith('INSERT INTO "MERGEAPPROVAL"'):
            (survivor, loser, plan_id, fp, version, tenant_id, status, snapshot) = params
            row = {
                "ApprovalID": self._next_approval_id, "SurvivorPaperID": survivor, "LoserPaperID": loser,
                "PlanID": plan_id, "PlanFingerprint": fp, "ApprovalVersion": version, "TenantID": tenant_id,
                "Status": status, "ReviewedByUserID": None, "ReviewedAt": None, "ReviewerNotes": None,
                "RevokedByUserID": None, "RevokedAt": None, "RevocationReason": None,
                "ExecutedAt": None, "ExecutionAuditLogID": None, "CreatedAt": datetime.now(timezone.utc),
            }
            self.approvals[self._next_approval_id] = row
            self._next_approval_id += 1
            self._result = [tuple(row[c] for c in ma.APPROVAL_COLUMNS)]
            return

        # --- MergeApproval: MAX version lookup ---
        if sql_upper.startswith('SELECT COALESCE(MAX'):
            survivor, loser, fp = params
            versions = [r["ApprovalVersion"] for r in self.approvals.values()
                        if (r["SurvivorPaperID"], r["LoserPaperID"], r["PlanFingerprint"]) == (survivor, loser, fp)]
            self._result = [(max(versions) if versions else 0,)]
            return

        # --- MergeApproval: fetch_current_approval lookup ---
        if 'FROM "MERGEAPPROVAL"' in sql_upper and sql_upper.startswith("SELECT"):
            survivor, loser, fp = params
            candidates = [r for r in self.approvals.values()
                          if (r["SurvivorPaperID"], r["LoserPaperID"], r["PlanFingerprint"]) == (survivor, loser, fp)]
            candidates.sort(key=lambda r: -r["ApprovalVersion"])
            self._result = [tuple(candidates[0][c] for c in ma.APPROVAL_COLUMNS)] if candidates else []
            return

        # --- final invariant check: SELECT "PaperID" FROM "ResearchPaper" WHERE "PaperID" = %s ---
        if norm == 'SELECT "PAPERID" FROM "RESEARCHPAPER" WHERE "PAPERID" = %S':
            (pid,) = params
            self._result = [(pid,)] if pid in self.research_papers else []
            return

        # --- lock_pair_rows: ResearchPaper FOR UPDATE ---
        if "FOR UPDATE" in sql_upper:
            ids = params[0]
            self._result = [(pid,) for pid in ids if pid in self.research_papers]
            return

        # --- merge_citation_fields SELECT (rp.PaperID, rp.CitationsByYear, COALESCE(...)) ---
        if "CITATIONSBYYEAR" in sql_upper and "COALESCE" in sql_upper and "SELECT" in sql_upper.split("FROM")[0]:
            ids = params[0]
            rows = [(pid, self.research_papers[pid].get("CitationsByYear"), self.research_papers[pid].get("_citations", 0))
                    for pid in ids if pid in self.research_papers]
            self._result = rows
            return

        # --- merge_citation_fields UPDATE ---
        if 'UPDATE "RESEARCHPAPER"' in sql_upper and "CITATIONSBYYEAR" in sql_upper:
            new_cby, total1, total2, total3, keep = params
            if keep in self.research_papers:
                self.research_papers[keep]["CitationsByYear"] = new_cby
                self.research_papers[keep]["_citations"] = total1
            self._result = []
            return

        # --- our JournalID backfill UPDATE ---
        if sql_upper.startswith('UPDATE "RESEARCHPAPER"') and 'SET "JOURNALID"' in sql_upper:
            new_journal_id, survivor_id = params
            if survivor_id in self.research_papers:
                self.research_papers[survivor_id]["JournalID"] = new_journal_id
            self._result = []
            return

        # --- ResearchPaper DELETE (loser) ---
        if sql_upper.startswith('DELETE FROM "RESEARCHPAPER"'):
            (pid,) = params
            if self.enforce_loser_paper_id_fk:
                referencing = [a["ApprovalID"] for a in self.approvals.values() if a["LoserPaperID"] == pid]
                if referencing:
                    raise SimulatedForeignKeyViolation(
                        f'update or delete on table "ResearchPaper" violates foreign key constraint '
                        f'on table "MergeApproval" -- Key (PaperID)=({pid}) is still referenced from '
                        f'table "MergeApproval" (ApprovalID(s) {referencing})'
                    )
            # SurvivorPaperID's FK (ON DELETE NO ACTION) was NEVER changed by
            # Phase 4K.1 -- unconditionally enforced here (no opt-in flag
            # needed, unlike the loser check above, since this reflects the
            # schema's real, unchanged, current state). No real code path
            # ever deletes a survivor row, so this can never fire in any
            # existing test -- it exists purely as the schema-level
            # defense-in-depth backstop the migration's own header comment
            # describes, and Phase 4K.1's report left untested.
            survivor_referencing = [a["ApprovalID"] for a in self.approvals.values() if a["SurvivorPaperID"] == pid]
            if survivor_referencing:
                raise SimulatedForeignKeyViolation(
                    f'update or delete on table "ResearchPaper" violates foreign key constraint '
                    f'on table "MergeApproval" -- Key (PaperID)=({pid}) is still referenced from '
                    f'table "MergeApproval" via SurvivorPaperID (ApprovalID(s) {survivor_referencing})'
                )
            self.research_papers.pop(pid, None)
            self._result = []
            return

        # --- fetch_current_state / build_papers_dict_for_pure_functions: COALESCE + ResearchPaper ---
        if "COALESCE" in sql_upper and 'FROM "RESEARCHPAPER"' in sql_upper:
            if "ANY(" in sql_upper:
                ids = params[0]
                rows = []
                for pid in ids:
                    r = self.research_papers.get(pid)
                    if r:
                        rows.append((pid, r.get("Title"), r.get("DOI"), r.get("PubYear"),
                                     r.get("IsVerified"), r.get("Source"), r.get("_citations", 0)))
                self._result = rows
                return
            (pid,) = params
            row = self.research_papers.get(pid)
            self._result = [(row.get("_citations", 0),)] if row else []
            return

        # --- is_doi_claimed_elsewhere ---
        if 'LOWER("DOI")' in sql_upper:
            doi, exclude = params
            hits = [pid for pid, r in self.research_papers.items()
                    if r.get("DOI") and str(r["DOI"]).lower() == str(doi).lower() and pid not in exclude]
            self._result = [(hits[0],)] if hits else []
            return

        # --- fetch_paper_row (fetch_current_state): full-column ResearchPaper select ---
        if 'FROM "RESEARCHPAPER"' in sql_upper and '"JOURNALID"' in sql_upper:
            (pid,) = params
            row = self.research_papers.get(pid)
            if not row:
                self._result = []
                return
            self._result = [tuple(row.get(c) for c in (["PaperID"] + RP_COLUMNS))]
            return

        # --- our post-merge_group AuditLog lookup ---
        if sql_upper.startswith('SELECT "LOGID" FROM "AUDITLOG"') and "DESC" in sql_upper:
            action, target_id = params
            matches = sorted(
                [r for r in self.audit_log if r[1] == target_id],
                key=lambda r: -r[0],
            )
            self._result = [(matches[0][0],)] if matches else []
            return

        # --- fetch_merge_audit_rows (idempotency preflight) ---
        if 'FROM "AUDITLOG"' in sql_upper:
            action, target_ids = params
            target_ids = set(target_ids)
            self._result = [(lid, tid, meta, created) for (lid, tid, meta, created) in self.audit_log
                             if tid in target_ids]
            return

        # --- merge_group: Authors INSERT ... SELECT ... ON CONFLICT DO NOTHING ---
        # (checked BEFORE the generic SELECT-shaped Authors routes below --
        # this INSERT's own column list literally contains "AuthorNameRaw",
        # so it would otherwise be misrouted as a plain SELECT.)
        if sql_upper.startswith('INSERT INTO "AUTHORS"'):
            keep, loser = params
            existing_keep_uids = {a["UserID"] for a in self.authors if a["PaperID"] == keep}
            for a in list(self.authors):
                if a["PaperID"] == loser and a["UserID"] not in existing_keep_uids:
                    self.authors.append(dict(a, PaperID=keep))
                    existing_keep_uids.add(a["UserID"])
            self._result = []
            return

        # --- merge_group: Authors DELETE (loser) ---
        if sql_upper.startswith('DELETE FROM "AUTHORS"'):
            (loser,) = params
            self.authors = [a for a in self.authors if a["PaperID"] != loser]
            self._result = []
            return

        # --- merge_group: DISTINCT UserID (expected_users, ANY / actual_users, scalar) ---
        if 'FROM "AUTHORS"' in sql_upper and "DISTINCT" in sql_upper:
            if "ANY(" in sql_upper:
                ids = set(params[0])
                uids = sorted({a["UserID"] for a in self.authors if a["PaperID"] in ids})
            else:
                (pid,) = params
                uids = sorted({a["UserID"] for a in self.authors if a["PaperID"] == pid})
            self._result = [(u,) for u in uids]
            return

        # --- build_papers_dict_for_pure_functions Authors query (bulk, AuthorOrder) ---
        if 'FROM "AUTHORS"' in sql_upper and "AUTHORORDER" in sql_upper and "ANY(" in sql_upper:
            ids = set(params[0])
            self._result = [(a["PaperID"], a["UserID"], a.get("AuthorOrder"))
                             for a in self.authors if a["PaperID"] in ids]
            return

        # --- fetch_authors_rows (fetch_current_state): UserID, AuthorNameRaw, scalar pid ---
        if 'FROM "AUTHORS"' in sql_upper and "AUTHORNAMERAW" in sql_upper:
            (pid,) = params
            self._result = [(a["UserID"], a["AuthorNameRaw"]) for a in self.authors if a["PaperID"] == pid]
            return

        # --- merge_group: AuditLog INSERT ---
        if sql_upper.startswith('INSERT INTO "AUDITLOG"'):
            action, target_type, target_id, metadata_json, source = params
            meta = json.loads(metadata_json)
            self.audit_log.append((self._next_audit_id, target_id, meta, datetime.now(timezone.utc)))
            self._next_audit_id += 1
            self._result = []
            return

        # --- existing_child_tables / check_unhandled_dependency_gaps: table existence ---
        if "INFORMATION_SCHEMA.TABLES" in sql_upper:
            (candidates,) = params
            self._result = [(t,) for t in candidates if t in self.existing_tables]
            return

        # --- authors_columns ---
        if "INFORMATION_SCHEMA.COLUMNS" in sql_upper:
            self._result = [(c,) for c in self.authors_column_list]
            return

        # --- check_unhandled_dependency_gaps: live COUNT queries ---
        if sql_upper.startswith('SELECT COUNT(*) FROM "AUTHORREVIEWQUEUE"'):
            (pid,) = params
            self._result = [(self.dependency_counts.get(("AuthorReviewQueue", pid), 0),)]
            return
        if sql_upper.startswith('SELECT COUNT(*) FROM "REPORTPAPERDECISION"'):
            (pid,) = params
            self._result = [(self.dependency_counts.get(("ReportPaperDecision", pid), 0),)]
            return

        raise AssertionError(f"ExecutorFakeCursor cannot route this SQL: {sql!r}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result or [])


# ===========================================================================
# Scenario fixtures
# ===========================================================================

CANARY_SURVIVOR, CANARY_LOSER = 5232, 5482
WINNER_AUTHORS = [{"PaperID": CANARY_SURVIVOR, "UserID": 97, "AuthorNameRaw": "H Alshammari, K Gasmi", "AuthorOrder": 1}]
LOSER_AUTHORS = [{"PaperID": CANARY_LOSER, "UserID": 97, "AuthorNameRaw": "H Alshammari, K Gasmi", "AuthorOrder": 1}]


def _fp(winner_row, loser_row, w_authors, l_authors, w_cit, l_cit):
    to_fp_authors = lambda rows: [{"UserID": a["UserID"], "AuthorNameRaw": a["AuthorNameRaw"]} for a in rows]
    return compute_plan_fingerprint(
        winner_row["PaperID"], loser_row["PaperID"], winner_row, loser_row,
        to_fp_authors(w_authors), to_fp_authors(l_authors), w_cit, l_cit,
    )


def _happy_path(journal_backfill=False, author_conflict=False, journal_conflict=False,
                 case_only_author_conflict=False,
                 existing_tables=(), dependency_counts=None, inject_failure_on=()):
    """Builds a fully-consistent, fully-approved, safe-to-execute scenario
    mirroring the real 5232/5482 canary pair -- unless a kwarg deliberately
    perturbs one dimension of it."""
    assert not (journal_backfill and journal_conflict), "mutually exclusive JournalID fixtures"
    assert not (author_conflict and case_only_author_conflict), "mutually exclusive author-conflict fixtures"
    winner_journal_id = 1803
    if journal_backfill:
        winner_journal_id = None
    loser_journal_id = None
    if journal_backfill:
        loser_journal_id = 555
    elif journal_conflict:
        loser_journal_id = 9999  # different, real, populated JournalID on both sides

    winner = rp_row(PaperID=CANARY_SURVIVOR, DOI="10.1155/2022/8531213",
                     JournalID=winner_journal_id,
                     Title="Optimal deep learning model", PubYear=2022, TenantID=1,
                     IsVerified=True, Source="Scholar", PublicationType="Research Article")
    loser = rp_row(PaperID=CANARY_LOSER, DOI=None, JournalID=loser_journal_id,
                    Title="Research Article Optimal deep learning model", PubYear=2022, TenantID=1,
                    IsVerified=True, Source="Scholar", PublicationType="Research Article")
    winner["_citations"] = 61
    loser["_citations"] = 0

    w_authors = [dict(a) for a in WINNER_AUTHORS]
    l_authors = [dict(a) for a in LOSER_AUTHORS]
    if author_conflict:
        l_authors[0]["AuthorNameRaw"] = "A completely different raw author string"
    if case_only_author_conflict:
        # Differs from WINNER_AUTHORS[0]'s "H Alshammari, K Gasmi" ONLY in
        # letter case -- Phase 4Z's proof fixture that a case-only label
        # still hits the executor's unconditional author-conflict block.
        l_authors[0]["AuthorNameRaw"] = "h alshammari, k gasmi"

    fp = _fp(winner, loser, w_authors, l_authors, winner["_citations"], loser["_citations"])

    cur = ExecutorFakeCursor(
        research_papers={CANARY_SURVIVOR: winner, CANARY_LOSER: loser},
        authors=w_authors + l_authors,
        existing_tables=set(existing_tables),
        dependency_counts=dependency_counts or {},
    )
    user = make_user()
    created = ma.create_pending_approval(cur, user, CANARY_SURVIVOR, CANARY_LOSER, "plan-4j", fp, tenant_id=1)
    assert created.ok, created.error
    with patch("merge_approval._write_audit"):
        approved = ma.approve_pending(cur, user, created.approval.approval_id, fp)
    assert approved.ok, approved.error
    cur.executed_sql = []  # reset -- setup traffic must not count toward "zero write SQL" assertions
    cur.inject_failure_on = tuple(inject_failure_on)  # only armed AFTER setup traffic completes
    return cur, user, fp


# ===========================================================================
# A. Successful, fully mocked canary execution
# ===========================================================================

class SuccessfulExecutionTests(unittest.TestCase):
    def test_canary_pair_executes_successfully(self):
        cur, user, fp = _happy_path()
        result = execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertTrue(result.ok, result.blocked_reason)
        self.assertEqual(result.approval_id, list(cur.approvals.keys())[0])
        self.assertIsNotNone(result.audit_log_id)

    def test_survivor_survives_loser_is_gone(self):
        cur, user, fp = _happy_path()
        execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertIn(CANARY_SURVIVOR, cur.research_papers)
        self.assertNotIn(CANARY_LOSER, cur.research_papers)

    def test_lock_order_is_ascending_regardless_of_survivor_loser_role(self):
        cur, user, fp = _happy_path()
        execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        for_update_sql = [s for s in cur.executed_sql if "FOR UPDATE" in s.upper() and 'FROM "RESEARCHPAPER"' in s.upper()]
        self.assertTrue(for_update_sql)

    def test_audit_log_written_and_linked(self):
        cur, user, fp = _happy_path()
        result = execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        matching = [r for r in cur.audit_log if r[0] == result.audit_log_id]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0][1], CANARY_LOSER)

    def test_approval_marked_executed(self):
        cur, user, fp = _happy_path()
        result = execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        approval_row = cur.approvals[result.approval_id]
        self.assertEqual(approval_row["Status"], ma.STATUS_EXECUTED)
        self.assertEqual(approval_row["ExecutionAuditLogID"], result.audit_log_id)

    def test_loser_delete_is_the_final_destructive_action(self):
        """The ResearchPaper DELETE is the last statement that removes or
        discards any data -- every write after it (only the MergeApproval
        EXECUTED bookkeeping update) is pure, non-destructive metadata
        cross-referencing, never a second data-loss opportunity. This
        ordering is a deliberate, documented choice (see merge_executor.py's
        module docstring): merge_group() is reused completely unmodified,
        so its own internal AuditLog-write-then-delete sequence can't be
        interleaved with anything -- the MergeApproval update necessarily
        comes after it, once merge_group() returns and the real AuditLog
        LogID it just wrote is known (needed for ExecutionAuditLogID)."""
        cur, user, fp = _happy_path()
        execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        write_statements = [s for s in cur.executed_sql if any(v in s.upper() for v in WRITE_VERBS)]
        self.assertTrue(write_statements)
        delete_index = next(i for i, s in enumerate(write_statements)
                             if s.strip().upper().startswith('DELETE FROM "RESEARCHPAPER"'))
        after_delete = write_statements[delete_index + 1:]
        self.assertTrue(all("DELETE" not in s.upper() for s in after_delete),
                         "no further DELETE may occur after the loser row is removed")
        for s in after_delete:
            self.assertTrue(s.strip().upper().startswith('UPDATE "MERGEAPPROVAL"'),
                             f"only the non-destructive MergeApproval bookkeeping update may follow the delete, found: {s!r}")


# ===========================================================================
# B-J. Each blocking scenario -- zero write SQL / rollback
# ===========================================================================

class BlockingScenarioTests(unittest.TestCase):
    def test_B_self_merge_zero_write_sql(self):
        cur, user, fp = _happy_path()
        result = execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_SURVIVOR, fp)
        self.assertFalse(result.ok)
        self.assertEqual(result.blocked_reason, EXEC_BLOCKED_SELF_MERGE)
        self.assertEqual(cur.executed_sql, [])

    def test_C_missing_approval_zero_write_sql(self):
        winner = rp_row(PaperID=1, DOI="10.1/x", Title="A paper", TenantID=1)
        loser = rp_row(PaperID=2, DOI=None, Title="A paper duplicate", TenantID=1)
        winner["_citations"] = loser["_citations"] = 0
        cur = ExecutorFakeCursor(research_papers={1: winner, 2: loser})
        result = execute_approved_merge(cur, make_user(), 1, 2, "some-fingerprint")
        self.assertFalse(result.ok)
        self.assertEqual(result.blocked_reason, EXEC_BLOCKED_NO_APPROVAL)
        self.assertFalse(_has_write(cur.executed_sql))

    def test_D_pending_approval_zero_write_sql(self):
        cur, user, fp = _happy_path()
        # Undo the approve step from the fixture -- leave the approval PENDING.
        (approval_id,) = cur.approvals.keys()
        cur.approvals[approval_id]["Status"] = ma.STATUS_PENDING
        result = execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertFalse(result.ok)
        self.assertEqual(result.blocked_reason, EXEC_BLOCKED_APPROVAL_NOT_APPROVED)
        self.assertFalse(_has_write(cur.executed_sql))

    def test_E_reversed_approval_zero_write_sql(self):
        """A defense-in-depth proof: even if fetch_current_approval() were
        ever made to return a mismatched-direction row (it cannot today --
        its own WHERE clause already prevents this), approval_matches_pair()
        independently catches it before any write."""
        cur, user, fp = _happy_path()
        reversed_row = ma.ApprovalRow(
            999, CANARY_LOSER, CANARY_SURVIVOR, "plan-x", fp, 1, 1, ma.STATUS_APPROVED,
            None, None, None, None, None, None, None, None, None,
        )
        with patch("merge_executor.fetch_current_approval", return_value=reversed_row):
            result = execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertFalse(result.ok)
        self.assertEqual(result.blocked_reason, EXEC_BLOCKED_APPROVAL_REVERSED)
        self.assertFalse(_has_write(cur.executed_sql))

    def test_F_stale_fingerprint_zero_write_sql(self):
        cur, user, fp = _happy_path()
        # Live data drifts AFTER approval was granted for `fp`.
        cur.research_papers[CANARY_SURVIVOR]["Title"] = "A totally different title now"
        result = execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertFalse(result.ok)
        self.assertEqual(result.blocked_reason, EXEC_BLOCKED_PREFLIGHT_FAILED)
        self.assertEqual(result.detail["preflight_status"], "STALE_FINGERPRINT")
        self.assertFalse(_has_write(cur.executed_sql))

    def test_G_already_executed_zero_write_sql(self):
        cur, user, fp = _happy_path()
        cur.audit_log.append((1, CANARY_LOSER, {"kept_paper_id": CANARY_SURVIVOR}, "t1"))
        result = execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertFalse(result.ok)
        self.assertEqual(result.blocked_reason, EXEC_BLOCKED_ALREADY_EXECUTED)
        self.assertFalse(_has_write(cur.executed_sql))

    def test_H_historical_state_ambiguous_zero_write_sql(self):
        cur, user, fp = _happy_path()
        cur.audit_log.append((1, CANARY_LOSER, None, "t1"))
        result = execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertFalse(result.ok)
        self.assertEqual(result.blocked_reason, EXEC_BLOCKED_HISTORY_AMBIGUOUS)
        self.assertFalse(_has_write(cur.executed_sql))

    def test_I_missing_row_after_lock_zero_destructive_writes(self):
        cur, user, fp = _happy_path()
        del cur.research_papers[CANARY_LOSER]  # simulate a concurrent prior deletion
        result = execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertFalse(result.ok)
        self.assertEqual(result.blocked_reason, EXEC_BLOCKED_MISSING_ROW)
        self.assertFalse(_has_write(cur.executed_sql))

    def test_J_child_remap_failure_rolls_back(self):
        cur, user, fp = _happy_path(inject_failure_on=('INSERT INTO "AUTHORS"',))
        with self.assertRaises(RuntimeError):
            execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        # The loser must still exist -- the DELETE never got a chance to run.
        self.assertIn(CANARY_LOSER, cur.research_papers)
        approval_id = list(cur.approvals.keys())[0]
        self.assertEqual(cur.approvals[approval_id]["Status"], ma.STATUS_APPROVED)


# ===========================================================================
# K/L. Field preservation
# ===========================================================================

class FieldPreservationTests(unittest.TestCase):
    def test_K_journal_id_deterministic_backfill(self):
        cur, user, fp = _happy_path(journal_backfill=True)
        result = execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertTrue(result.ok, result.blocked_reason)
        self.assertEqual(cur.research_papers[CANARY_SURVIVOR]["JournalID"], 555)

    def test_L_author_name_raw_conflict_blocks_before_merge(self):
        cur, user, fp = _happy_path(author_conflict=True)
        result = execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertFalse(result.ok)
        self.assertEqual(result.blocked_reason, EXEC_BLOCKED_AUTHOR_CONFLICT)
        self.assertFalse(_has_write(cur.executed_sql))
        self.assertIn(CANARY_LOSER, cur.research_papers, "loser must survive a blocked run")

    def test_phase_4z_case_only_conflict_also_blocks_before_merge(self):
        """Phase 4Z, Task F item 3 -- the central safety invariant of this
        phase: CASE_ONLY_FORMATTING_DIFFERENCE != execution permission.
        execute_approved_merge() re-derives author_content_conflicts()
        live at Step 15 and has no awareness of is_case_only_difference()
        or author_conflict_type at all -- it treats this fixture exactly
        like any other non-empty conflict list."""
        cur, user, fp = _happy_path(case_only_author_conflict=True)
        w_raw, l_raw = "H Alshammari, K Gasmi", "h alshammari, k gasmi"
        is_case_only, _ = is_case_only_difference(w_raw, l_raw)
        self.assertTrue(is_case_only, "fixture must actually be a case-only difference")

        result = execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertFalse(result.ok)
        self.assertEqual(result.blocked_reason, EXEC_BLOCKED_AUTHOR_CONFLICT)
        self.assertFalse(_has_write(cur.executed_sql))
        self.assertIn(CANARY_LOSER, cur.research_papers, "loser must survive a blocked run")

    def test_phase_4z_reviewed_approved_state_still_does_not_unblock_executor(self):
        """Phase 4Z, Task F item 3, the full proof: even after the pure
        review state machine independently reaches REVIEWED_APPROVED for
        this exact case-only conflict, execute_approved_merge() -- which
        never reads that state, was never passed it, and has no code path
        that consults it -- still blocks identically. The two systems are
        provably disconnected, not merely undocumented as disconnected."""
        cur, user, fp = _happy_path(case_only_author_conflict=True)
        approvals_before = {k: dict(v) for k, v in cur.approvals.items()}
        papers_before = {k: dict(v) for k, v in cur.research_papers.items()}

        review_state, _ = advance_case_only_review(REVIEW_STATE_HUMAN_REVIEW_REQUIRED, "APPROVE")
        self.assertEqual(review_state, REVIEW_STATE_REVIEWED_APPROVED)

        # Task F items 5/6/7: the review call itself (a pure function with
        # no cur/DB parameter at all) cannot have touched MergeApproval or
        # ResearchPaper -- confirmed directly against the mock cursor.
        self.assertEqual({k: dict(v) for k, v in cur.approvals.items()}, approvals_before)
        self.assertEqual({k: dict(v) for k, v in cur.research_papers.items()}, papers_before)

        result = execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertFalse(result.ok)
        self.assertEqual(result.blocked_reason, EXEC_BLOCKED_AUTHOR_CONFLICT)
        self.assertFalse(_has_write(cur.executed_sql))

    def test_journal_id_conflict_state_blocks_before_merge(self):
        """Phase 4K, scenario 5: added because the audit found this exact
        state (both sides populated with DIFFERENT real JournalIDs) had
        never been exercised at the execute_approved_merge() level --
        EXEC_BLOCKED_JOURNAL_CONFLICT was imported into this test file but
        never actually asserted anywhere. build_journal_state()'s own
        CONFLICT classification is independently proven correct elsewhere
        (dedup_papers.py's JournalIdDecisionModel tests); this proves the
        executor's own wiring -- that a CONFLICT state actually blocks
        execution, with zero write SQL, before any child remap or delete."""
        cur, user, fp = _happy_path(journal_conflict=True)
        result = execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertFalse(result.ok)
        self.assertEqual(result.blocked_reason, EXEC_BLOCKED_JOURNAL_CONFLICT)
        self.assertEqual(result.detail["journal_state"]["state"], "CONFLICT")
        self.assertFalse(_has_write(cur.executed_sql))
        self.assertIn(CANARY_LOSER, cur.research_papers, "loser must survive a blocked run")


# ===========================================================================
# M/N/O/P. Write-time failures -- must roll back, never report false success
# ===========================================================================

class WriteFailureTests(unittest.TestCase):
    def test_M_approval_executed_update_failure_rolls_back(self):
        cur, user, fp = _happy_path(inject_failure_on=('EXECUTIONAUDITLOGID',))
        with self.assertRaises(RuntimeError):
            execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)

    def test_N_auditlog_failure_rolls_back(self):
        cur, user, fp = _happy_path(inject_failure_on=('INSERT INTO "AUDITLOG"',))
        with self.assertRaises(RuntimeError):
            execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertIn(CANARY_LOSER, cur.research_papers)

    def test_O_delete_failure_rolls_back(self):
        cur, user, fp = _happy_path(inject_failure_on=('DELETE FROM "RESEARCHPAPER"',))
        with self.assertRaises(RuntimeError):
            execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertIn(CANARY_LOSER, cur.research_papers)
        approval_id = list(cur.approvals.keys())[0]
        # AuditLog write happened (inside merge_group(), before the delete),
        # but the loser row itself is proven still present above -- no
        # partial "merged" state is externally observable as a success.

    def test_P_unexpected_mid_preflight_exception_not_reported_as_success(self):
        cur, user, fp = _happy_path(inject_failure_on=('LOWER("DOI")',))
        with self.assertRaises(RuntimeError):
            execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertFalse(_has_write(cur.executed_sql), "no write may occur before a mid-preflight failure")


# ===========================================================================
# Q. No COMMIT before every required operation succeeds
# ===========================================================================

class TransactionOwnershipTests(unittest.TestCase):
    def _source(self):
        with open(mx.__file__, encoding="utf-8") as f:
            return f.read()

    def test_module_never_calls_commit_or_rollback(self):
        src = self._source()
        self.assertNotIn(".commit(", src)
        self.assertNotIn(".rollback(", src)

    def test_module_never_opens_its_own_transaction(self):
        src = self._source()
        self.assertNotIn("transaction.atomic", src)

    def test_write_statements_only_occur_after_every_preflight_check_in_source_order(self):
        """Source-order proxy: every preflight function call must appear
        BEFORE the first write-shaped literal SQL string in the file."""
        src = self._source()
        preflight_markers = [
            "reject_self_merge(", "can_approve_merge(", "fetch_current_approval(",
            "lock_pair_rows(", "fetch_current_state(", "validate_against_plan(",
            "idempotency_verdict(", "build_journal_state(", "author_content_conflicts(",
            "check_unhandled_dependency_gaps(",
        ]
        first_write_offset = src.index('UPDATE "ResearchPaper" SET "JournalID"')
        for marker in preflight_markers:
            offset = src.index(marker)
            self.assertLess(offset, first_write_offset,
                             f"{marker!r} must appear before the first write statement in source order")


# ===========================================================================
# Dependency / FK action matrix
# ===========================================================================

class DependencyActionMatrixTests(unittest.TestCase):
    def test_every_matrix_entry_has_a_valid_action(self):
        valid = {mx.DEP_ACTION_REMAP, mx.DEP_ACTION_SET_NULL, mx.DEP_ACTION_AUTOMATIC_CASCADE, mx.DEP_ACTION_BLOCK}
        for table, fk, rule, action, note in DEPENDENCY_ACTION_MATRIX:
            self.assertIn(action, valid)

    def test_previously_flagged_gaps_are_classified_block(self):
        by_key = {(t, fk): action for t, fk, _rule, action, _note in DEPENDENCY_ACTION_MATRIX}
        self.assertEqual(by_key[("AuthorReviewQueue", "PaperID")], DEP_ACTION_BLOCK)
        self.assertEqual(by_key[("ReportPaperDecision", "MissingResolvedToPaperID")], DEP_ACTION_BLOCK)

    def test_merge_group_handled_dependencies_are_classified_remap(self):
        by_key = {(t, fk): action for t, fk, _rule, action, _note in DEPENDENCY_ACTION_MATRIX}
        for key in [("Authors", "PaperID"), ("Citations", "PaperID"), ("ExternalAuthors", "PaperID"),
                    ("CitationsHistory", "PaperID"), ("ReportPaperDecision", "PaperID")]:
            self.assertEqual(by_key[key], DEP_ACTION_REMAP)

    def test_zero_rows_never_blocks(self):
        cur = ExecutorFakeCursor(existing_tables={"AuthorReviewQueue", "ReportPaperDecision"},
                                  dependency_counts={})
        blockers = check_unhandled_dependency_gaps(cur, loser_id=5482)
        self.assertEqual(blockers, [])

    def test_nonexistent_table_never_blocks_without_querying_it(self):
        """Matches existing_child_tables()'s own convention: a table that
        doesn't exist is never even COUNT-queried, let alone assumed to
        contain rows."""
        cur = ExecutorFakeCursor(existing_tables=set())
        blockers = check_unhandled_dependency_gaps(cur, loser_id=5482)
        self.assertEqual(blockers, [])
        self.assertFalse(any('"AUTHORREVIEWQUEUE"' in s.upper() and "COUNT" in s.upper() for s in cur.executed_sql))

    def test_nonzero_authorreviewqueue_rows_block(self):
        cur = ExecutorFakeCursor(existing_tables={"AuthorReviewQueue"},
                                  dependency_counts={("AuthorReviewQueue", 5482): 3})
        blockers = check_unhandled_dependency_gaps(cur, loser_id=5482)
        self.assertEqual(len(blockers), 1)
        self.assertEqual(blockers[0]["table"], "AuthorReviewQueue")
        self.assertEqual(blockers[0]["row_count"], 3)

    def test_nonzero_report_paper_decision_missing_resolved_rows_block(self):
        cur = ExecutorFakeCursor(existing_tables={"ReportPaperDecision"},
                                  dependency_counts={("ReportPaperDecision", 5482): 1})
        blockers = check_unhandled_dependency_gaps(cur, loser_id=5482)
        self.assertEqual(len(blockers), 1)
        self.assertEqual(blockers[0]["table"], "ReportPaperDecision")

    def test_full_pipeline_blocks_on_dependency_gap(self):
        cur, user, fp = _happy_path(existing_tables={"AuthorReviewQueue"},
                                     dependency_counts={("AuthorReviewQueue", CANARY_LOSER): 2})
        result = execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertFalse(result.ok)
        self.assertEqual(result.blocked_reason, EXEC_BLOCKED_DEPENDENCY_GAP)
        self.assertFalse(_has_write(cur.executed_sql))
        self.assertIn(CANARY_LOSER, cur.research_papers)


# ===========================================================================
# Permission
# ===========================================================================

class PermissionTests(unittest.TestCase):
    def test_permission_denied_zero_write_sql(self):
        cur, _admin_user, fp = _happy_path()
        other_user = make_user(is_admin=False, perms=[])
        result = execute_approved_merge(cur, other_user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertFalse(result.ok)
        self.assertEqual(result.blocked_reason, mx.EXEC_BLOCKED_PERMISSION_DENIED)
        self.assertFalse(_has_write(cur.executed_sql))


# ===========================================================================
# Phase 4U -- cross-tenant enforcement, boundary #2 of 2 (the executor,
# immediately before any write). Real, confirmed defect (Task A of
# backend/reports/phase4u_cross_tenant_enforcement.md): no code anywhere in
# the approval/execution path checked TenantID equality between survivor
# and loser before this phase. Boundary #1 (create_pending_approval())
# is tested in test_merge_approval.py's CrossTenantApprovalCreationTests.
# ===========================================================================

def _cross_tenant_scenario():
    """Simulates a row that somehow already exists as cross-tenant --
    inserted directly into the fake cursor's approval store, NOT through
    create_pending_approval() (which, after this phase's fix, would
    correctly refuse to create this exact row in the first place). This is
    deliberate: it tests the executor's OWN independent enforcement
    boundary, the one that must catch an approval predating the fix, or
    created by any future caller that bypasses create_pending_approval()
    entirely -- exactly the defense-in-depth scenario Task B's threat
    model identifies."""
    winner = rp_row(PaperID=CANARY_SURVIVOR, DOI="10.1155/2022/8531213", JournalID=1803,
                     Title="Optimal deep learning model", PubYear=2022, TenantID=1,
                     IsVerified=True, Source="Scholar", PublicationType="Research Article")
    loser = rp_row(PaperID=CANARY_LOSER, DOI=None, JournalID=None,
                    Title="Research Article Optimal deep learning model", PubYear=2022, TenantID=2,
                    IsVerified=True, Source="Scholar", PublicationType="Research Article")
    winner["_citations"] = 61
    loser["_citations"] = 0
    w_authors = [dict(a) for a in WINNER_AUTHORS]
    l_authors = [dict(a) for a in LOSER_AUTHORS]
    fp = _fp(winner, loser, w_authors, l_authors, winner["_citations"], loser["_citations"])

    cur = ExecutorFakeCursor(
        research_papers={CANARY_SURVIVOR: winner, CANARY_LOSER: loser},
        authors=w_authors + l_authors,
    )
    cur.approvals[1] = {
        "ApprovalID": 1, "SurvivorPaperID": CANARY_SURVIVOR, "LoserPaperID": CANARY_LOSER,
        "PlanID": "cross-tenant-test", "PlanFingerprint": fp, "ApprovalVersion": 1,
        "TenantID": 1, "Status": ma.STATUS_APPROVED, "ReviewedByUserID": 221,
        "ReviewedAt": None, "ReviewerNotes": None, "RevokedByUserID": None,
        "RevokedAt": None, "RevocationReason": None, "ExecutedAt": None,
        "ExecutionAuditLogID": None, "CreatedAt": None,
    }
    cur._next_approval_id = 2
    return cur, make_user(), fp


class CrossTenantExecutionTests(unittest.TestCase):
    def test_F_cross_tenant_approved_pair_blocked(self):
        cur, user, fp = _cross_tenant_scenario()
        result = execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertFalse(result.ok)
        self.assertEqual(result.blocked_reason, mx.EXEC_BLOCKED_CROSS_TENANT)

    def test_G_block_before_any_child_table_write(self):
        cur, user, fp = _cross_tenant_scenario()
        execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertFalse(any('"AUTHORS"' in s.upper() and any(v in s.upper() for v in WRITE_VERBS)
                              for s in cur.executed_sql))

    def test_H_block_before_auditlog_merge_insert(self):
        cur, user, fp = _cross_tenant_scenario()
        execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertFalse(any('INSERT INTO "AUDITLOG"' in s.upper() for s in cur.executed_sql))

    def test_I_block_before_loser_delete(self):
        cur, user, fp = _cross_tenant_scenario()
        execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertIn(CANARY_LOSER, cur.research_papers, "loser must still exist -- blocked, not deleted")

    def test_J_block_before_approval_executed(self):
        cur, user, fp = _cross_tenant_scenario()
        execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertEqual(cur.approvals[1]["Status"], ma.STATUS_APPROVED,
                          "approval must remain APPROVED, never falsely EXECUTED")

    def test_zero_write_sql_at_all(self):
        cur, user, fp = _cross_tenant_scenario()
        execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertFalse(_has_write(cur.executed_sql), "a cross-tenant pair must never reach any write")

    def test_K_same_tenant_canary_shaped_pair_unchanged(self):
        """Confirms the fix does not accidentally block a legitimate,
        same-tenant pair -- the real canary shape still succeeds."""
        cur, user, fp = _happy_path()
        result = execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertTrue(result.ok, result.blocked_reason)


class CrossTenantCheckReachabilityTests(unittest.TestCase):
    """Static + behavioral proof the check is reachable from
    execute_approved_merge() itself, not merely from
    merge_plan_generator.py (which execute_approved_merge() never calls)."""

    def test_validate_same_tenant_imported_and_actually_called(self):
        import inspect
        import re
        src = inspect.getsource(mx)
        self.assertIn("validate_same_tenant", src)
        calls = list(re.finditer(r'validate_same_tenant\(', src))
        self.assertGreaterEqual(len(calls), 1,
                                 "validate_same_tenant must be CALLED in merge_executor.py, not merely imported")

    def test_reachable_by_real_behavior_not_only_source_scan(self):
        cur, user, fp = _cross_tenant_scenario()
        result = execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertEqual(result.blocked_reason, mx.EXEC_BLOCKED_CROSS_TENANT)


# ===========================================================================
# Static / structural safety checks
# ===========================================================================

class StaticSafetyChecks(unittest.TestCase):
    def _source(self):
        with open(mx.__file__, encoding="utf-8") as f:
            return f.read()

    def test_no_network_client(self):
        src = self._source()
        for forbidden in ("requests.", "urllib.request", "httpx.", "socket.", "http.client"):
            self.assertNotIn(forbidden, src)

    def test_no_apply_flag_or_subprocess(self):
        src = self._source()
        self.assertNotIn("subprocess", src)
        self.assertNotIn("os.system", src)
        self.assertNotIn("add_argument(\"--apply\"", src)

    def test_executor_cannot_run_without_approval_by_construction(self):
        """fetch_current_approval() must be called, and its result checked
        for None, strictly before any write-shaped literal SQL string."""
        src = self._source()
        first_write_offset = src.index('UPDATE "ResearchPaper" SET "JournalID"')
        approval_fetch_offset = src.index("fetch_current_approval(cur")
        approval_none_check_offset = src.index("if approval is None:")
        self.assertLess(approval_fetch_offset, first_write_offset)
        self.assertLess(approval_none_check_offset, first_write_offset)

    def test_no_direct_doi_column_write(self):
        src = self._source()
        self.assertNotIn('SET "DOI"', src)
        self.assertNotIn('"DOI" = %s', src.replace("\n", " "))

    def test_no_dedup_papers_or_merge_plan_generator_modification_markers(self):
        """This file must never redefine merge_group's own internal SQL --
        proof by absence of any raw write-verb SQL literal targeting
        Authors/Citations/AuditLog/ResearchPaper OTHER than the two writes
        this phase's own design doc explicitly adds (JournalID backfill,
        MergeApproval EXECUTED)."""
        src = self._source()
        self.assertNotIn("INSERT INTO \"AUTHORS\"", src)
        self.assertNotIn("DELETE FROM \"AUTHORS\"", src)
        self.assertNotIn("INSERT INTO \"AUDITLOG\"", src)


if __name__ == "__main__":
    unittest.main()
