"""Tests for merge_approval.py -- Phase 4I. Pure-function tests plus a
stateful in-memory mock cursor for the thin DB-facing wrappers. No live DB
connection, no network, no writes to any real database. accounts.common.audit
is never actually called -- merge_approval._write_audit() is patched out in
every test that exercises a state transition, so no Django DB connection is
opened by this suite at all.

Run: cd backend && python tools/test_merge_approval.py
"""
import os
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import merge_approval as ma  # noqa: E402
from merge_approval import (  # noqa: E402
    ApprovalRow,
    OperationResult,
    STATUS_APPROVED,
    STATUS_EXECUTED,
    STATUS_PENDING,
    STATUS_REJECTED,
    STATUS_REVOKED,
    approval_matches_pair,
    approve_pending,
    can_approve_merge,
    create_pending_approval,
    fetch_current_approval,
    is_legal_transition,
    reject_pending,
    revoke_approved,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

def make_user(user_id=1, tenant_id=1, is_admin=True, perms=(), authenticated=True):
    perm_set = set(perms)
    return SimpleNamespace(
        user_id=user_id,
        tenant_id=tenant_id,
        user_type='Admin' if is_admin else 'Researcher',
        is_authenticated=authenticated,
        has_litrix_perm=lambda code: code in perm_set,
    )


class InMemoryApprovalCursor:
    """Simulates a real MergeApproval table well enough to exercise every
    function in merge_approval.py, including the UNIQUE constraint and
    NOT NULL/self-merge CHECK from sprint11_merge_approval.sql, without a
    real database. Raises AssertionError on any write-verb SQL that isn't
    an explicit, expected INSERT/UPDATE against "MergeApproval" -- a second,
    execution-time safety net alongside the static source scan."""

    def __init__(self, research_papers=None):
        self.rows = {}  # ApprovalID -> dict of APPROVAL_COLUMNS
        self._next_id = 1
        self._result = None
        self.executed_sql = []
        # Phase 4U: PaperID -> TenantID, for create_pending_approval()'s new
        # fetch_paper_tenant_ids() call. None (the default) means "every
        # PaperID exists with TenantID=1" -- preserves every pre-Phase-4U
        # test's behavior unchanged without editing each of them. Tests
        # exercising a missing-paper or cross-tenant scenario pass an
        # explicit dict instead.
        self.research_papers = research_papers

    def execute(self, sql, params=None):
        self.executed_sql.append(sql)
        sql_upper = sql.upper()

        if sql_upper.startswith('SELECT "PAPERID", "TENANTID" FROM "RESEARCHPAPER"'):
            (ids,) = params
            if self.research_papers is None:
                self._result = [(pid, 1) for pid in ids]
            else:
                self._result = [(pid, self.research_papers[pid]) for pid in ids if pid in self.research_papers]
            return

        if sql_upper.startswith("INSERT INTO \"MERGEAPPROVAL\""):
            (survivor, loser, plan_id, fp, version, tenant_id, status, snapshot) = params
            # Mirror the real UNIQUE constraint.
            for r in self.rows.values():
                if (r["SurvivorPaperID"], r["LoserPaperID"], r["PlanFingerprint"], r["ApprovalVersion"]) \
                        == (survivor, loser, fp, version):
                    raise AssertionError("UNIQUE constraint uq_merge_approval_identity violated")
            if survivor == loser:
                raise AssertionError("CHECK constraint chk_merge_approval_not_self violated")
            row = {
                "ApprovalID": self._next_id, "SurvivorPaperID": survivor, "LoserPaperID": loser,
                "PlanID": plan_id, "PlanFingerprint": fp, "ApprovalVersion": version,
                "TenantID": tenant_id, "Status": status, "ReviewedByUserID": None,
                "ReviewedAt": None, "ReviewerNotes": None, "RevokedByUserID": None,
                "RevokedAt": None, "RevocationReason": None, "ExecutedAt": None,
                "ExecutionAuditLogID": None, "CreatedAt": datetime.now(timezone.utc),
            }
            self.rows[self._next_id] = row
            self._next_id += 1
            self._result = [tuple(row[c] for c in ma.APPROVAL_COLUMNS)]
            return

        if sql_upper.startswith("SELECT COALESCE(MAX"):
            survivor, loser, fp = params
            versions = [r["ApprovalVersion"] for r in self.rows.values()
                        if (r["SurvivorPaperID"], r["LoserPaperID"], r["PlanFingerprint"]) == (survivor, loser, fp)]
            self._result = [(max(versions) if versions else 0,)]
            return

        if "FOR UPDATE" in sql_upper:
            (approval_id,) = params
            row = self.rows.get(approval_id)
            self._result = [tuple(row[c] for c in ma.APPROVAL_COLUMNS)] if row else []
            return

        if sql_upper.startswith("UPDATE \"MERGEAPPROVAL\""):
            *set_params, approval_id = params
            row = self.rows[approval_id]
            if "\"STATUS\" = %S, \"REVIEWEDBYUSERID\"" in sql_upper:
                status, reviewer, notes = set_params
                row["Status"], row["ReviewedByUserID"], row["ReviewerNotes"] = status, reviewer, notes
                row["ReviewedAt"] = datetime.now(timezone.utc)
            elif "\"STATUS\" = %S, \"REVOKEDBYUSERID\"" in sql_upper:
                status, revoker, reason = set_params
                row["Status"], row["RevokedByUserID"], row["RevocationReason"] = status, revoker, reason
                row["RevokedAt"] = datetime.now(timezone.utc)
            else:
                raise AssertionError(f"unrecognized UPDATE shape: {sql!r}")
            self._result = [tuple(row[c] for c in ma.APPROVAL_COLUMNS)]
            return

        if sql_upper.startswith("SELECT") and 'FROM "MERGEAPPROVAL"' in sql_upper:
            survivor, loser, fp = params
            candidates = [r for r in self.rows.values()
                          if (r["SurvivorPaperID"], r["LoserPaperID"], r["PlanFingerprint"]) == (survivor, loser, fp)]
            candidates.sort(key=lambda r: -r["ApprovalVersion"])
            self._result = [tuple(candidates[0][c] for c in ma.APPROVAL_COLUMNS)] if candidates else []
            return

        raise AssertionError(f"InMemoryApprovalCursor cannot route this SQL: {sql!r}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result or [])


AUDIT_PATCH_TARGET = "merge_approval._write_audit"


# ---------------------------------------------------------------------------
# Pure state-machine tests
# ---------------------------------------------------------------------------

class StateMachineTests(unittest.TestCase):
    def test_pending_to_approved_is_legal(self):
        self.assertTrue(is_legal_transition(STATUS_PENDING, STATUS_APPROVED))

    def test_pending_to_rejected_is_legal(self):
        self.assertTrue(is_legal_transition(STATUS_PENDING, STATUS_REJECTED))

    def test_approved_to_revoked_is_legal(self):
        self.assertTrue(is_legal_transition(STATUS_APPROVED, STATUS_REVOKED))

    def test_approved_to_executed_is_legal(self):
        self.assertTrue(is_legal_transition(STATUS_APPROVED, STATUS_EXECUTED))

    def test_pending_to_revoked_is_illegal(self):
        self.assertFalse(is_legal_transition(STATUS_PENDING, STATUS_REVOKED))

    def test_rejected_to_approved_is_illegal(self):
        self.assertFalse(is_legal_transition(STATUS_REJECTED, STATUS_APPROVED))

    def test_executed_has_no_outgoing_transitions(self):
        self.assertFalse(is_legal_transition(STATUS_EXECUTED, STATUS_REVOKED))
        self.assertFalse(is_legal_transition(STATUS_EXECUTED, STATUS_APPROVED))


class ApprovalMatchesPairTests(unittest.TestCase):
    def _approval(self, survivor, loser):
        return ApprovalRow(1, survivor, loser, "p1", "fp1", 1, 1, STATUS_APPROVED,
                            None, None, None, None, None, None, None, None, None)

    def test_matching_direction(self):
        self.assertTrue(approval_matches_pair(self._approval(5232, 5482), 5232, 5482))

    def test_reversed_direction_does_not_match(self):
        """5232 survives/5482 loses is NOT the same as the reverse."""
        self.assertFalse(approval_matches_pair(self._approval(5232, 5482), 5482, 5232))

    def test_none_approval_never_matches(self):
        self.assertFalse(approval_matches_pair(None, 5232, 5482))


class PermissionTests(unittest.TestCase):
    def test_admin_can_approve(self):
        self.assertTrue(can_approve_merge(make_user(is_admin=True)))

    def test_manage_users_perm_can_approve(self):
        self.assertTrue(can_approve_merge(make_user(is_admin=False, perms=['manage_users'])))

    def test_unauthenticated_cannot_approve(self):
        self.assertFalse(can_approve_merge(make_user(authenticated=False)))

    def test_ordinary_user_without_perm_cannot_approve(self):
        self.assertFalse(can_approve_merge(make_user(is_admin=False, perms=[])))


# ---------------------------------------------------------------------------
# 1. create_pending_approval
# ---------------------------------------------------------------------------

class CreatePendingApprovalTests(unittest.TestCase):
    def test_create_pending(self):
        cur = InMemoryApprovalCursor()
        user = make_user()
        result = create_pending_approval(cur, user, 5232, 5482, "plan-1", "fp-abc", tenant_id=1)
        self.assertTrue(result.ok)
        self.assertEqual(result.approval.status, STATUS_PENDING)
        self.assertEqual(result.approval.approval_version, 1)

    def test_self_merge_rejected_before_any_sql(self):
        cur = InMemoryApprovalCursor()
        user = make_user()
        result = create_pending_approval(cur, user, 5232, 5232, "plan-1", "fp-abc", tenant_id=1)
        self.assertFalse(result.ok)
        self.assertIn("self_merge_rejected", result.error)
        self.assertEqual(cur.executed_sql, [])

    def test_permission_denied(self):
        cur = InMemoryApprovalCursor()
        user = make_user(is_admin=False, perms=[])
        result = create_pending_approval(cur, user, 5232, 5482, "plan-1", "fp-abc", tenant_id=1)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "permission_denied")

    def test_duplicate_active_approval_returns_existing_not_a_new_row(self):
        cur = InMemoryApprovalCursor()
        user = make_user()
        first = create_pending_approval(cur, user, 5232, 5482, "plan-1", "fp-abc", tenant_id=1)
        second = create_pending_approval(cur, user, 5232, 5482, "plan-1", "fp-abc", tenant_id=1)
        self.assertTrue(second.ok)
        self.assertEqual(second.approval.approval_id, first.approval.approval_id)
        self.assertEqual(len(cur.rows), 1, "no duplicate row may be created")

    def test_new_pending_after_rejected_gets_a_new_version(self):
        cur = InMemoryApprovalCursor()
        user = make_user()
        first = create_pending_approval(cur, user, 5232, 5482, "plan-1", "fp-abc", tenant_id=1)
        with patch(AUDIT_PATCH_TARGET):
            reject_pending(cur, user, first.approval.approval_id, "fp-abc")
        second = create_pending_approval(cur, user, 5232, 5482, "plan-2", "fp-abc", tenant_id=1)
        self.assertTrue(second.ok)
        self.assertEqual(second.approval.approval_version, 2)
        self.assertNotEqual(second.approval.approval_id, first.approval.approval_id)

    def test_cannot_create_new_pending_after_executed(self):
        cur = InMemoryApprovalCursor()
        user = make_user()
        pending = create_pending_approval(cur, user, 5232, 5482, "plan-1", "fp-abc", tenant_id=1)
        cur.rows[pending.approval.approval_id]["Status"] = STATUS_EXECUTED  # simulate a completed execution
        result = create_pending_approval(cur, user, 5232, 5482, "plan-2", "fp-abc", tenant_id=1)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "already_executed")

    def test_different_fingerprint_is_a_wholly_separate_identity(self):
        cur = InMemoryApprovalCursor()
        user = make_user()
        create_pending_approval(cur, user, 5232, 5482, "plan-1", "fp-abc", tenant_id=1)
        second = create_pending_approval(cur, user, 5232, 5482, "plan-2", "fp-different", tenant_id=1)
        self.assertTrue(second.ok)
        self.assertEqual(second.approval.approval_version, 1, "a new fingerprint starts its own version sequence")
        self.assertEqual(len(cur.rows), 2)


# ---------------------------------------------------------------------------
# Phase 4U -- cross-tenant enforcement, boundary #1 (approval creation).
# Real, confirmed defect (backend/reports/phase4u_cross_tenant_enforcement.md,
# Task A): create_pending_approval() never checked that SurvivorPaperID and
# LoserPaperID belong to the same tenant before this phase.
# ---------------------------------------------------------------------------

class CrossTenantApprovalCreationTests(unittest.TestCase):
    def test_A_same_tenant_pair_existing_behavior_preserved(self):
        cur = InMemoryApprovalCursor(research_papers={5232: 1, 5482: 1})
        user = make_user()
        result = create_pending_approval(cur, user, 5232, 5482, "plan-1", "fp-abc", tenant_id=1)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.approval.status, STATUS_PENDING)

    def test_B_cross_tenant_pair_rejected(self):
        cur = InMemoryApprovalCursor(research_papers={5232: 1, 5482: 2})
        user = make_user()
        result = create_pending_approval(cur, user, 5232, 5482, "plan-1", "fp-abc", tenant_id=1)
        self.assertFalse(result.ok)
        self.assertIn("cross_tenant_rejected", result.error)

    def test_C_cross_tenant_rejection_zero_insert(self):
        cur = InMemoryApprovalCursor(research_papers={5232: 1, 5482: 2})
        user = make_user()
        create_pending_approval(cur, user, 5232, 5482, "plan-1", "fp-abc", tenant_id=1)
        self.assertEqual(len(cur.rows), 0, "no MergeApproval row may exist after a cross-tenant rejection")
        self.assertFalse(any(s.upper().startswith('INSERT INTO "MERGEAPPROVAL"') for s in cur.executed_sql),
                          "zero INSERT statements may be issued")

    def test_D_missing_survivor_safe_failure(self):
        cur = InMemoryApprovalCursor(research_papers={5482: 1})  # 5232 does not exist
        user = make_user()
        result = create_pending_approval(cur, user, 5232, 5482, "plan-1", "fp-abc", tenant_id=1)
        self.assertFalse(result.ok)
        self.assertIn("cross_tenant_rejected", result.error)
        self.assertEqual(len(cur.rows), 0)

    def test_E_missing_loser_safe_failure(self):
        cur = InMemoryApprovalCursor(research_papers={5232: 1})  # 5482 does not exist
        user = make_user()
        result = create_pending_approval(cur, user, 5232, 5482, "plan-1", "fp-abc", tenant_id=1)
        self.assertFalse(result.ok)
        self.assertIn("cross_tenant_rejected", result.error)
        self.assertEqual(len(cur.rows), 0)


# ---------------------------------------------------------------------------
# 2/3/4. approve / reject / revoke
# ---------------------------------------------------------------------------

class TransitionTests(unittest.TestCase):
    def _pending(self, cur=None, user=None, survivor=5232, loser=5482, fp="fp-abc"):
        cur = cur or InMemoryApprovalCursor()
        user = user or make_user()
        result = create_pending_approval(cur, user, survivor, loser, "plan-1", fp, tenant_id=1)
        return cur, user, result.approval

    def test_approve_pending(self):
        cur, user, approval = self._pending()
        with patch(AUDIT_PATCH_TARGET) as mock_audit:
            result = approve_pending(cur, user, approval.approval_id, approval.plan_fingerprint, notes="looks good")
        self.assertTrue(result.ok)
        self.assertEqual(result.approval.status, STATUS_APPROVED)
        self.assertEqual(result.approval.reviewed_by_user_id, user.user_id)
        self.assertIsNotNone(result.approval.reviewed_at)
        mock_audit.assert_called_once()

    def test_reject_pending(self):
        cur, user, approval = self._pending()
        with patch(AUDIT_PATCH_TARGET):
            result = reject_pending(cur, user, approval.approval_id, approval.plan_fingerprint, notes="not safe")
        self.assertTrue(result.ok)
        self.assertEqual(result.approval.status, STATUS_REJECTED)

    def test_revoke_approved(self):
        cur, user, approval = self._pending()
        with patch(AUDIT_PATCH_TARGET):
            approve_pending(cur, user, approval.approval_id, approval.plan_fingerprint)
            result = revoke_approved(cur, user, approval.approval_id, approval.plan_fingerprint, reason="data changed")
        self.assertTrue(result.ok)
        self.assertEqual(result.approval.status, STATUS_REVOKED)
        self.assertEqual(result.approval.revoked_by_user_id, user.user_id)

    def test_illegal_transition_pending_to_revoked(self):
        cur, user, approval = self._pending()
        with patch(AUDIT_PATCH_TARGET) as mock_audit:
            result = revoke_approved(cur, user, approval.approval_id, approval.plan_fingerprint)
        self.assertFalse(result.ok)
        self.assertIn("illegal_transition", result.error)
        mock_audit.assert_not_called()

    def test_illegal_transition_double_approve_is_rejected(self):
        """Simulates the sequential outcome of two concurrent approve
        attempts racing on SELECT ... FOR UPDATE -- whichever commits
        first wins, the second sees the already-updated Status and is
        correctly refused, proving the lock+guard combination prevents a
        double transition rather than silently succeeding twice."""
        cur, user, approval = self._pending()
        with patch(AUDIT_PATCH_TARGET):
            first = approve_pending(cur, user, approval.approval_id, approval.plan_fingerprint)
            second = approve_pending(cur, user, approval.approval_id, approval.plan_fingerprint)
        self.assertTrue(first.ok)
        self.assertFalse(second.ok)
        self.assertIn("illegal_transition", second.error)

    def test_fingerprint_mismatch_blocks_approval(self):
        """The reviewer must be deciding on the EXACT plan they saw --
        passing a stale/wrong fingerprint must never silently approve
        whatever is currently stored."""
        cur, user, approval = self._pending()
        with patch(AUDIT_PATCH_TARGET) as mock_audit:
            result = approve_pending(cur, user, approval.approval_id, "a-different-fingerprint")
        self.assertFalse(result.ok)
        self.assertIn("fingerprint_mismatch", result.error)
        mock_audit.assert_not_called()

    def test_permission_denied_on_transition(self):
        cur, _, approval = self._pending()
        other_user = make_user(is_admin=False, perms=[])
        result = approve_pending(cur, other_user, approval.approval_id, approval.plan_fingerprint)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "permission_denied")

    def test_not_found(self):
        cur = InMemoryApprovalCursor()
        user = make_user()
        result = approve_pending(cur, user, 999999, "fp-abc")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "not_found")

    def test_reversed_pair_is_a_different_approval_row_entirely(self):
        """Approving {survivor=5232, loser=5482} must never be reachable by
        anyone looking up/acting on {survivor=5482, loser=5232} -- proven
        here by showing the reversed pair's lookup finds nothing."""
        cur, user, approval = self._pending(survivor=5232, loser=5482)
        with patch(AUDIT_PATCH_TARGET):
            approve_pending(cur, user, approval.approval_id, approval.plan_fingerprint)
        reversed_lookup = fetch_current_approval(cur, 5482, 5232, approval.plan_fingerprint)
        self.assertIsNone(reversed_lookup)
        self.assertFalse(approval_matches_pair(
            fetch_current_approval(cur, 5232, 5482, approval.plan_fingerprint), 5482, 5232))


# ---------------------------------------------------------------------------
# 5. fetch_current_approval
# ---------------------------------------------------------------------------

class FetchCurrentApprovalTests(unittest.TestCase):
    def test_no_approval_returns_none(self):
        cur = InMemoryApprovalCursor()
        self.assertIsNone(fetch_current_approval(cur, 5232, 5482, "fp-abc"))

    def test_returns_highest_version(self):
        cur = InMemoryApprovalCursor()
        user = make_user()
        create_pending_approval(cur, user, 5232, 5482, "plan-1", "fp-abc", tenant_id=1)
        first = fetch_current_approval(cur, 5232, 5482, "fp-abc")
        with patch(AUDIT_PATCH_TARGET):
            reject_pending(cur, user, first.approval_id, "fp-abc")
        create_pending_approval(cur, user, 5232, 5482, "plan-2", "fp-abc", tenant_id=1)
        latest = fetch_current_approval(cur, 5232, 5482, "fp-abc")
        self.assertEqual(latest.approval_version, 2)

    def test_changed_fingerprint_invalidates_lookup_reuse(self):
        """An approval for one fingerprint must never be found (let alone
        reused) when looking up a DIFFERENT fingerprint for the same pair."""
        cur = InMemoryApprovalCursor()
        user = make_user()
        create_pending_approval(cur, user, 5232, 5482, "plan-1", "fp-original", tenant_id=1)
        self.assertIsNotNone(fetch_current_approval(cur, 5232, 5482, "fp-original"))
        self.assertIsNone(fetch_current_approval(cur, 5232, 5482, "fp-changed"))


# ---------------------------------------------------------------------------
# Audit integration
# ---------------------------------------------------------------------------

class AuditIntegrationTests(unittest.TestCase):
    def test_creation_is_not_audited(self):
        """Matches the real repository precedent exactly: grep-confirmed
        this phase, backend/analytics/disambiguation/pipeline.py (which
        creates the real AuthorReviewQueue rows) contains zero audit()
        calls -- only the human DECISION is audited there, and this module
        follows that precedent, not an invented one."""
        cur = InMemoryApprovalCursor()
        user = make_user()
        with patch(AUDIT_PATCH_TARGET) as mock_audit:
            create_pending_approval(cur, user, 5232, 5482, "plan-1", "fp-abc", tenant_id=1)
        mock_audit.assert_not_called()

    def test_approve_is_audited_with_correct_target(self):
        cur, user, approval = TransitionTests()._pending()
        with patch(AUDIT_PATCH_TARGET) as mock_audit:
            approve_pending(cur, user, approval.approval_id, approval.plan_fingerprint)
        mock_audit.assert_called_once()
        call_args = mock_audit.call_args
        self.assertEqual(call_args.args[3], approval.approval_id)  # target_id positional
        self.assertEqual(call_args.args[2], "merge_approval.approved")  # action positional

    def test_reject_is_audited(self):
        cur, user, approval = TransitionTests()._pending()
        with patch(AUDIT_PATCH_TARGET) as mock_audit:
            reject_pending(cur, user, approval.approval_id, approval.plan_fingerprint)
        mock_audit.assert_called_once()

    def test_revoke_is_audited(self):
        cur, user, approval = TransitionTests()._pending()
        with patch(AUDIT_PATCH_TARGET):
            approve_pending(cur, user, approval.approval_id, approval.plan_fingerprint)
        with patch(AUDIT_PATCH_TARGET) as mock_audit:
            revoke_approved(cur, user, approval.approval_id, approval.plan_fingerprint)
        mock_audit.assert_called_once()

    def test_merge_approval_never_reads_status_from_auditlog(self):
        """MergeApproval.Status is authoritative on its own -- this module
        never queries AuditLog to determine current approval state."""
        import inspect
        src = inspect.getsource(ma)
        self.assertNotIn('FROM "AuditLog"', src)


# ---------------------------------------------------------------------------
# 14. Canary identity (Survivor=5232, Loser=5482) -- mocked, not live
# ---------------------------------------------------------------------------

class CanaryIdentityTests(unittest.TestCase):
    """Exercises the full create -> approve lifecycle for the exact
    Phase 4G/4H/4G-final-confirmation canary identity, using the real
    fingerprint value already established live in those phases
    (unchanged, re-confirmed again this phase in Section F of the report --
    same value, byte-for-byte)."""

    CANARY_FINGERPRINT = "2298ea25fc1c53b842809926bc72a5e0e77ec566e04b4f83f214a85544d705cb"

    def test_canary_full_lifecycle(self):
        cur = InMemoryApprovalCursor()
        user = make_user()

        created = create_pending_approval(
            cur, user, survivor_id=5232, loser_id=5482,
            plan_id="canary-plan-4g", plan_fingerprint=self.CANARY_FINGERPRINT, tenant_id=1)
        self.assertTrue(created.ok)
        self.assertEqual(created.approval.survivor_paper_id, 5232)
        self.assertEqual(created.approval.loser_paper_id, 5482)
        self.assertEqual(created.approval.status, STATUS_PENDING)

        with patch(AUDIT_PATCH_TARGET):
            approved = approve_pending(cur, user, created.approval.approval_id, self.CANARY_FINGERPRINT)
        self.assertTrue(approved.ok)
        self.assertEqual(approved.approval.status, STATUS_APPROVED)

        found = fetch_current_approval(cur, 5232, 5482, self.CANARY_FINGERPRINT)
        self.assertEqual(found.status, STATUS_APPROVED)
        self.assertTrue(approval_matches_pair(found, 5232, 5482))
        self.assertFalse(approval_matches_pair(found, 5482, 5232))


# ---------------------------------------------------------------------------
# Static source-scan (explicit non-goals, Task F-equivalent for this module)
# ---------------------------------------------------------------------------

class NonGoalsStaticSourceScan(unittest.TestCase):
    def _source(self):
        with open(ma.__file__, encoding="utf-8") as f:
            return f.read()

    def test_no_merge_group_import_or_call(self):
        src = self._source()
        for line in src.splitlines():
            if line.strip().startswith(("import ", "from ")):
                self.assertNotIn("merge_group", line)
        import re
        for m in re.finditer(r'merge_group\(', src):
            self.assertEqual(src[m.end():m.end() + 1], ")")

    def test_no_delete_against_research_paper(self):
        src = self._source()
        self.assertNotIn('DELETE FROM "ResearchPaper"', src)
        self.assertNotIn("DELETE FROM \"ResearchPaper\"", src)

    def test_no_doi_column_writes(self):
        src = self._source()
        self.assertNotIn('"DOI" = ', src)
        self.assertNotIn('SET "DOI"', src)

    def test_no_apply_flag(self):
        src = self._source()
        self.assertNotIn("--apply", src)

    def test_no_write_sql_outside_mergeapproval_table(self):
        import re
        src = self._source()
        sql_calls = re.findall(r'cur\.execute\(\s*(?:f?)([\'"]{1,3})(.*?)\1', src, re.DOTALL)
        self.assertGreater(len(sql_calls), 0)
        write_verbs = ("INSERT ", "UPDATE ", "DELETE ", "TRUNCATE ", "ALTER ", "DROP ", "CREATE ")
        for _quote, sql in sql_calls:
            sql_upper = sql.upper()
            for verb in write_verbs:
                if verb in sql_upper:
                    self.assertIn("MERGEAPPROVAL", sql_upper.replace('"', '').upper(),
                                  f"write verb {verb!r} found targeting a table other than MergeApproval: {sql!r}")

    def test_no_child_table_remap_or_journal_backfill_logic(self):
        src = self._source()
        for forbidden_table in ('"Authors"', '"Citations"', '"ExternalAuthors"',
                                 '"CitationsHistory"', '"ReportPaperDecision"', '"AuthorReviewQueue"'):
            self.assertNotIn(forbidden_table, src)


if __name__ == "__main__":
    unittest.main()
