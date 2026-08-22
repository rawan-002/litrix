"""Phase 4K.1 -- MergeApproval FK lifecycle validation.

Phase 4K proved, from documented PostgreSQL foreign-key semantics plus a
live schema read, that the ORIGINAL MergeApproval design (both
SurvivorPaperID and LoserPaperID as ON DELETE NO ACTION) is
self-contradictory: an APPROVED MergeApproval row is the unconditional
precondition for reaching execution, and that same row's LoserPaperID
reference makes Postgres refuse to DELETE the loser ResearchPaper row while
it exists. Phase 4K.1's fix removes LoserPaperID's foreign key entirely
(sprint11_merge_approval.sql), matching the live, already-production-proven
AuditLog.TargetID precedent (confirmed via information_schema this phase
and Phase 4K to carry no FK constraint at all).

HONEST LIMITATION, stated per this phase's own explicit instruction not to
fake PostgreSQL semantics and claim full validation: no real, safely
isolated PostgreSQL instance was available in this environment (no local
psql/postgres binary, no second database configured -- only the production
Neon connection via DATABASE_URL exists, and this phase is forbidden from
touching it with anything but read-only SELECTs). What follows is a
Python-level SIMULATION of one specific, well-documented, deterministic
Postgres rule -- ExecutorFakeCursor.enforce_loser_paper_id_fk (added this
phase in test_merge_executor.py): a DELETE against a row referenced by an
undeferred ON DELETE NO ACTION constraint fails immediately if a
referencing row exists. This is not "faking away" the complexity -- it IS
the real rule, encoded directly and used to reproduce the EXACT Phase 4K
failure mode before proving the fix removes it. What this CANNOT prove:
real Postgres error codes/messages, transaction isolation-level
interactions, DEFERRABLE constraint timing under concurrent load, or index
behavior. A real disposable PostgreSQL instance remains the stronger
validation this file's tests recommend before that migration is applied to
any database.

Phase 4L (end-to-end audit of the existing Phase 4K.1 design -- NOT a
design change) extends this file with the remaining lifecycle properties
Phase 4K.1 itself did not directly test: SurvivorPaperID's own FK
protection (never exercised, since no code path ever deletes a survivor),
historical-approval readability through the REAL lookup function (not just
direct dict access into the test double), the static absence of any
ResearchPaper join in the approval-lookup code path, and the operational
vs. historical state distinction this design deliberately draws: a
PENDING/APPROVED approval requires BOTH PaperIDs to be live, current rows
(enforced by execute_approved_merge()'s own preflight, unchanged); an
EXECUTED approval is a historical record and LoserPaperID intentionally
may no longer correspond to any live ResearchPaper row -- by design, not
by accident.

Run: cd backend && python tools/test_fk_lifecycle.py
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import merge_approval as ma  # noqa: E402
from merge_approval import fetch_current_approval  # noqa: E402
from merge_executor import EXEC_BLOCKED_MISSING_ROW, execute_approved_merge  # noqa: E402
from test_merge_executor import (  # noqa: E402
    CANARY_LOSER,
    CANARY_SURVIVOR,
    ExecutorFakeCursor,
    SimulatedForeignKeyViolation,
    _fp,
    _has_write,
    make_user,
    rp_row,
    WINNER_AUTHORS,
    LOSER_AUTHORS,
)


def _build_approved_scenario(enforce_loser_paper_id_fk):
    """Identical to test_merge_executor.py's _happy_path(), reproduced here
    (not imported) so this file's one scenario is self-contained and its
    FK-mode parameter is explicit at the call site, not buried in a shared
    default. Mirrors the real 5232/5482 canary shape exactly."""
    winner = rp_row(PaperID=CANARY_SURVIVOR, DOI="10.1155/2022/8531213", JournalID=1803,
                     Title="Optimal deep learning model", PubYear=2022, TenantID=1,
                     IsVerified=True, Source="Scholar", PublicationType="Research Article")
    loser = rp_row(PaperID=CANARY_LOSER, DOI=None, JournalID=None,
                    Title="Research Article Optimal deep learning model", PubYear=2022, TenantID=1,
                    IsVerified=True, Source="Scholar", PublicationType="Research Article")
    winner["_citations"] = 61
    loser["_citations"] = 0

    w_authors = [dict(a) for a in WINNER_AUTHORS]
    l_authors = [dict(a) for a in LOSER_AUTHORS]
    fp = _fp(winner, loser, w_authors, l_authors, winner["_citations"], loser["_citations"])

    cur = ExecutorFakeCursor(
        research_papers={CANARY_SURVIVOR: winner, CANARY_LOSER: loser},
        authors=w_authors + l_authors,
        enforce_loser_paper_id_fk=enforce_loser_paper_id_fk,
    )
    user = make_user()
    created = ma.create_pending_approval(cur, user, CANARY_SURVIVOR, CANARY_LOSER, "plan-4k1", fp, tenant_id=1)
    assert created.ok, created.error
    with patch("merge_approval._write_audit"):
        approved = ma.approve_pending(cur, user, created.approval.approval_id, fp)
    assert approved.ok, approved.error
    approval_id = created.approval.approval_id
    cur.executed_sql = []
    return cur, user, fp, approval_id


class OriginalDesignReproducesThePhase4KFailure(unittest.TestCase):
    """Proves the mock is faithful, not merely asserting the fix "works" in
    isolation: under the ORIGINAL (pre-4K.1) FK design, the exact failure
    Phase 4K predicted analytically actually reproduces here."""

    def test_delete_fails_with_simulated_fk_violation(self):
        cur, user, fp, approval_id = _build_approved_scenario(enforce_loser_paper_id_fk=True)
        with self.assertRaises(SimulatedForeignKeyViolation) as ctx:
            execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertIn("MergeApproval", str(ctx.exception))
        self.assertIn(str(CANARY_LOSER), str(ctx.exception))

    def test_failure_leaves_no_partial_corruption(self):
        """Requirement 6's rollback test: loser remains, approval does not
        incorrectly become EXECUTED, no partial historical corruption."""
        cur, user, fp, approval_id = _build_approved_scenario(enforce_loser_paper_id_fk=True)
        with self.assertRaises(SimulatedForeignKeyViolation):
            execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertIn(CANARY_LOSER, cur.research_papers, "loser must still exist -- the DELETE never succeeded")
        self.assertEqual(cur.approvals[approval_id]["Status"], ma.STATUS_APPROVED,
                          "approval must remain APPROVED, never falsely EXECUTED")
        self.assertIsNone(cur.approvals[approval_id]["ExecutedAt"])
        self.assertIsNone(cur.approvals[approval_id]["ExecutionAuditLogID"])
        # The real, in-memory-only fidelity gap, stated honestly (matching
        # Phase 4J's M/N/O/P tests' own disclaimer): merge_group()'s Authors
        # remap and AuditLog INSERT already mutated this fake cursor's
        # in-memory state before the DELETE raised, because this mock has
        # no transaction/rollback mechanism of its own. Real atomic
        # rollback -- undoing ALL of that, not just the delete -- is
        # Django's transaction.atomic()'s job, proven sound by source
        # inspection (Phase 4K §5) and by the exception-propagation proof
        # above, not re-proven by this in-memory mock.


class CorrectedDesignSucceeds(unittest.TestCase):
    """The Phase 4K.1 fix: LoserPaperID carries no FK -- the identical
    scenario now completes cleanly end to end."""

    def test_full_lifecycle_reaches_executed_with_no_fk_violation(self):
        cur, user, fp, approval_id = _build_approved_scenario(enforce_loser_paper_id_fk=False)
        result = execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertTrue(result.ok, result.blocked_reason)
        self.assertEqual(cur.approvals[approval_id]["Status"], ma.STATUS_EXECUTED)

    def test_loser_actually_deleted(self):
        cur, user, fp, approval_id = _build_approved_scenario(enforce_loser_paper_id_fk=False)
        execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertNotIn(CANARY_LOSER, cur.research_papers)
        self.assertIn(CANARY_SURVIVOR, cur.research_papers)

    def test_approval_history_survives_the_loser_deletion(self):
        """Requirement 6, item 5/8: the approval row itself must still
        exist, and the historical identity of the merged pair -- which
        PaperID was the loser -- must remain directly recoverable from it,
        with no NULL, no reconstruction via a second table required."""
        cur, user, fp, approval_id = _build_approved_scenario(enforce_loser_paper_id_fk=False)
        execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        surviving_approval = cur.approvals[approval_id]
        self.assertEqual(surviving_approval["SurvivorPaperID"], CANARY_SURVIVOR)
        self.assertEqual(surviving_approval["LoserPaperID"], CANARY_LOSER,
                          "the loser's real PaperID must still be directly readable -- "
                          "not NULLed, not lost, requiring no cross-reference to reconstruct")
        self.assertEqual(surviving_approval["Status"], ma.STATUS_EXECUTED)

    def test_execution_audit_log_id_cross_reference_also_recoverable(self):
        """A second, independent way to answer 'which pair was merged' --
        MergeApproval.ExecutionAuditLogID -> AuditLog.TargetID (also
        unconstrained, live-confirmed) -- happens to agree, but is not
        required to, since LoserPaperID alone is now sufficient."""
        cur, user, fp, approval_id = _build_approved_scenario(enforce_loser_paper_id_fk=False)
        result = execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        audit_row = next(r for r in cur.audit_log if r[0] == result.audit_log_id)
        self.assertEqual(audit_row[1], CANARY_LOSER)

    def test_historical_approval_readable_through_the_real_lookup_function(self):
        """Phase 4L item 2: 'the historical approval record remains
        readable after execution' -- proven through fetch_current_approval()
        itself (the real, production lookup function), not merely by
        inspecting the test double's internal dict directly."""
        cur, user, fp, approval_id = _build_approved_scenario(enforce_loser_paper_id_fk=False)
        execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        found = fetch_current_approval(cur, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertIsNotNone(found)
        self.assertEqual(found.status, ma.STATUS_EXECUTED)
        self.assertEqual(found.loser_paper_id, CANARY_LOSER)


class SurvivorPaperIdIntegrityProtected(unittest.TestCase):
    """Phase 4L item 7: SurvivorPaperID's own FK (ON DELETE NO ACTION) was
    never touched by Phase 4K.1 and remains real -- but no code path in
    this project has ever exercised it, since nothing ever deletes a
    survivor. This proves the schema-level backstop the migration's own
    header comment describes actually holds, using the SAME
    ExecutorFakeCursor mechanism (test_merge_executor.py), now applied
    unconditionally to SurvivorPaperID rather than gated behind the
    loser-specific opt-in flag."""

    def test_deleting_a_referenced_survivor_is_blocked(self):
        cur, user, fp, approval_id = _build_approved_scenario(enforce_loser_paper_id_fk=False)
        with self.assertRaises(SimulatedForeignKeyViolation) as ctx:
            cur.execute('DELETE FROM "ResearchPaper" WHERE "PaperID" = %s', (CANARY_SURVIVOR,))
        self.assertIn("SurvivorPaperID", str(ctx.exception))
        self.assertIn(CANARY_SURVIVOR, cur.research_papers, "the survivor row must still exist -- blocked, not deleted")


class OperationalVsHistoricalStateDistinction(unittest.TestCase):
    """Phase 4L's core design contract, made explicit as tests: PENDING/
    APPROVED is an OPERATIONAL state (both PaperIDs must be live, current
    rows before execution is even attempted) -- EXECUTED is a HISTORICAL
    state (LoserPaperID intentionally may no longer reference a live row).
    These are deliberately different requirements for different states,
    not an inconsistency."""

    def test_survivor_missing_before_execution_blocks_with_zero_writes(self):
        """Item 4/5: pre-execution validation requires BOTH PaperIDs live --
        Phase 4J's existing test_I already proves this for a missing LOSER;
        this is the missing symmetric case (missing SURVIVOR) that no prior
        phase's test suite exercised."""
        cur, user, fp, approval_id = _build_approved_scenario(enforce_loser_paper_id_fk=False)
        del cur.research_papers[CANARY_SURVIVOR]  # simulate the survivor vanishing before execution
        result = execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertFalse(result.ok)
        self.assertEqual(result.blocked_reason, EXEC_BLOCKED_MISSING_ROW)
        self.assertFalse(_has_write(cur.executed_sql))

    def test_orphaned_approval_cannot_silently_bypass_execution_safety(self):
        """Item 8: because LoserPaperID no longer has a live FK, NOTHING in
        the schema itself prevents some OTHER, unrelated code path (a
        manual admin deletion, a since-removed cleanup script -- not
        execute_approved_merge() or merge_group()) from deleting a
        ResearchPaper row that a PENDING or APPROVED MergeApproval still
        references. This proves that possibility is not dangerous: the
        executor's own live preflight (fetch_current_state(), re-run fresh
        under lock, completely independent of any FK) still catches it and
        refuses to execute -- an orphaned OPERATIONAL-state approval blocks
        cleanly, it does not silently proceed as if nothing were wrong."""
        cur, user, fp, approval_id = _build_approved_scenario(enforce_loser_paper_id_fk=False)
        self.assertEqual(cur.approvals[approval_id]["Status"], ma.STATUS_APPROVED)
        # Simulate the orphaning: the loser ResearchPaper row disappears
        # through some path OTHER than this approval's own intended
        # execution -- possible precisely because no FK stops it.
        del cur.research_papers[CANARY_LOSER]
        result = execute_approved_merge(cur, user, CANARY_SURVIVOR, CANARY_LOSER, fp)
        self.assertFalse(result.ok)
        self.assertEqual(result.blocked_reason, EXEC_BLOCKED_MISSING_ROW)
        self.assertFalse(_has_write(cur.executed_sql), "an orphaned approval must never be silently executed")
        # The approval itself is untouched by the refused attempt -- still
        # APPROVED, not corrupted into some invalid intermediate state.
        self.assertEqual(cur.approvals[approval_id]["Status"], ma.STATUS_APPROVED)


class NoImplicitResearchPaperJoinInApprovalLookup(unittest.TestCase):
    """Phase 4L item 3, verified statically: the approval-lookup code path
    must never assume a live join to ResearchPaper is safe (it wouldn't be,
    for an EXECUTED row's LoserPaperID) -- proven here by confirming no
    such join exists anywhere in the source, not merely by one test
    scenario happening not to trigger one."""

    def test_no_sql_join_against_researchpaper_in_merge_approval_module(self):
        import inspect
        import re
        src = inspect.getsource(ma)
        # Python's str.join() calls (e.g. ', '.join(...)) are expected and
        # must not be mistaken for a SQL JOIN -- only a real SQL keyword,
        # bounded by word edges and not immediately preceded by a dot, counts.
        sql_joins = [m for m in re.finditer(r'(?<!\.)\bJOIN\b', src, re.IGNORECASE)]
        self.assertEqual(sql_joins, [], "merge_approval.py must never JOIN against ResearchPaper")


if __name__ == "__main__":
    unittest.main()
