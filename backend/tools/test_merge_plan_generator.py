"""Tests for merge_plan_generator.py's pure planning logic (no DB, no
network). Mirrors test_dedup_papers.py's convention.

Run: cd backend && python tools/test_merge_plan_generator.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from merge_plan_generator import (  # noqa: E402
    CHILD_TABLE_SPECS,
    build_author_conflict_report,
    build_case_only_review_display,
    build_child_table_actions,
    build_doi_state,
    build_field_actions,
    build_journal_state,
    build_survivor_reason,
    classify_field,
    compute_classification,
    generate_pair_plan,
    plan_child_action,
    advance_case_only_review,
    CASE_ONLY_REVIEW_DISCLAIMER,
    REVIEW_STATE_PLAN_GENERATED,
    REVIEW_STATE_CASE_ONLY_FORMATTING_DIFFERENCE,
    REVIEW_STATE_HUMAN_REVIEW_REQUIRED,
    REVIEW_STATE_REVIEWED_APPROVED,
    REVIEW_STATE_REVIEWED_REJECTED,
    STATUS_CONFLICT,
    STATUS_COPY_LOSER,
    STATUS_EMPTY_BOTH,
    STATUS_EQUAL,
    STATUS_HUMAN_REVIEW,
    STATUS_KEEP_WINNER,
    STATUS_MERGE,
    STATUS_UNKNOWN,
)


def rp_row(**overrides):
    """A minimal winner/loser ResearchPaper row -- every column defaults to
    None so a test only needs to specify what it cares about."""
    from merge_plan_generator import RP_COLUMNS
    row = {c: None for c in RP_COLUMNS}
    row["PaperID"] = overrides.pop("PaperID", 1)
    row.update(overrides)
    return row


class SurvivorSelectionReporting(unittest.TestCase):
    """Survivor selection itself is never reinvented here -- this module
    only labels the choose_keep_reason it's handed."""

    def test_has_doi_is_labeled_as_proven_by_repository(self):
        reason = build_survivor_reason("has_doi")
        self.assertIn("has a populated DOI", reason)
        self.assertIn("PROVEN BY REPOSITORY", reason)

    def test_unrecognized_reason_does_not_silently_pass(self):
        reason = build_survivor_reason("something_new")
        self.assertIn("unrecognized", reason)


class FieldClassificationBasics(unittest.TestCase):
    def test_equal_values(self):
        status, reason, action = classify_field("Language", "en", "en")
        self.assertEqual(status, STATUS_EQUAL)

    def test_empty_both(self):
        status, _, _ = classify_field("Abstract", None, "")
        self.assertEqual(status, STATUS_EMPTY_BOTH)

    def test_winner_only_value_is_kept_no_loser_contribution(self):
        status, _, action = classify_field("Abstract", "some text", None)
        self.assertEqual(status, STATUS_KEEP_WINNER)
        self.assertEqual(action, "NO_ACTION")

    def test_loser_only_value_is_copy_candidate(self):
        status, _, action = classify_field("Abstract", None, "loser has this")
        self.assertEqual(status, STATUS_COPY_LOSER)
        self.assertEqual(action, "BACKFILL_FROM_LOSER")

    def test_genuine_conflict_has_no_deterministic_rule(self):
        status, reason, action = classify_field("PublicationType", "Conference Paper", "Research Article")
        self.assertEqual(status, STATUS_CONFLICT)
        self.assertEqual(action, "HUMAN_DECISION_REQUIRED")
        self.assertIn("no repository-backed deterministic rule", reason)

    def test_tenant_id_conflict_is_flagged_for_pair_level_block(self):
        status, reason, action = classify_field("TenantID", 1, 2)
        self.assertEqual(status, STATUS_CONFLICT)
        self.assertEqual(action, "BLOCK_PAIR")

    def test_raw_data_log_loser_only_is_human_review_not_auto_copy(self):
        status, _, action = classify_field("RawData_Log", None, {"cited_by_count": 5})
        self.assertEqual(status, STATUS_HUMAN_REVIEW)

    def test_citations_by_year_conflict_is_merge_not_conflict(self):
        status, reason, action = classify_field(
            "CitationsByYear", {"2020": 1}, {"2020": 5, "2021": 2})
        self.assertEqual(status, STATUS_MERGE)
        self.assertIn("merge_citation_fields", reason)

    def test_citations_by_year_equal_is_equal(self):
        status, _, _ = classify_field("CitationsByYear", {"2020": 1}, {"2020": 1})
        self.assertEqual(status, STATUS_EQUAL)


class JournalIdSpecificCases(unittest.TestCase):
    """Phase 4B.1's specifically-flagged planning gap -- JournalID must
    never be silently dropped from the report.

    Phase 4E, Gap 1: JournalID now gets its own dedicated decision object
    (journal_state, built from dedup_papers.py's journal_id_decision()) --
    same deferral pattern DOI already used via doi_state. classify_field()
    for JournalID now always defers to SEE_JOURNAL_STATE (STATUS_UNKNOWN),
    intentionally changed from the prior generic COPY_LOSER/CONFLICT verdict
    it used to return -- the real verdict lives in journal_state instead,
    tested separately below."""

    def test_journal_id_field_action_always_defers_to_journal_state(self):
        for winner_val, loser_val in ((None, 771), (953, None), (100, 200), (100, 100), (None, None)):
            status, _, action = classify_field("JournalID", winner_val, loser_val)
            self.assertIn(status, (STATUS_UNKNOWN, STATUS_EQUAL, STATUS_EMPTY_BOTH))
            self.assertNotEqual(status, STATUS_COPY_LOSER)
            self.assertNotEqual(status, STATUS_CONFLICT)

    def test_journal_id_always_present_in_field_actions(self):
        winner = rp_row(PaperID=1)
        loser = rp_row(PaperID=2, JournalID=771)
        actions = build_field_actions(winner, loser)
        fields = [a["field"] for a in actions]
        self.assertIn("JournalID", fields)
        ja = next(a for a in actions if a["field"] == "JournalID")
        self.assertEqual(ja["recommended_action"], "SEE_JOURNAL_STATE")


class JournalStateDecisionShapes(unittest.TestCase):
    """Phase 4E, Gap 1 -- the five required JournalID shapes, at the plan
    level (build_journal_state), not just dedup_papers.py's raw decision."""

    def test_both_null(self):
        state = build_journal_state(None, None)
        self.assertEqual(state["state"], "NO_JOURNAL")
        self.assertTrue(state["execution_permitted"])

    def test_winner_only(self):
        state = build_journal_state(953, None)
        self.assertEqual(state["state"], "WINNER_ONLY")
        self.assertTrue(state["execution_permitted"])

    def test_loser_only_is_explicit_backfill_plan(self):
        state = build_journal_state(None, 771)
        self.assertEqual(state["state"], "LOSER_ONLY_BACKFILL")
        self.assertTrue(state["execution_permitted"])
        self.assertIn("backfill", state["planned_action"].lower())
        self.assertEqual(state["loser_value"], 771)

    def test_equal_values(self):
        state = build_journal_state(676, 676)
        self.assertEqual(state["state"], "EQUAL")
        self.assertTrue(state["execution_permitted"])

    def test_conflicting_values_blocks_execution_and_does_not_choose(self):
        state = build_journal_state(100, 200)
        self.assertEqual(state["state"], "CONFLICT")
        self.assertFalse(state["execution_permitted"])
        self.assertIsNotNone(state["blocking_reason"])
        self.assertEqual(state["winner_value"], 100)
        self.assertEqual(state["loser_value"], 200)

    def test_phase_4y_no_unintended_impact_on_journal_id_logic(self):
        """Task F item 16: Phase 4Y touches only AuthorNameRaw conflict
        labeling; build_journal_state()/journal_id_decision() are untouched
        by this phase. Re-asserts the full 5-shape matrix unchanged,
        including the real LOSER_ONLY_BACKFILL shape shared by all three
        Phase 4Y candidate pairs."""
        self.assertEqual(build_journal_state(None, None)["state"], "NO_JOURNAL")
        self.assertEqual(build_journal_state(953, None)["state"], "WINNER_ONLY")
        self.assertEqual(build_journal_state(None, 771)["state"], "LOSER_ONLY_BACKFILL")
        self.assertEqual(build_journal_state(676, 676)["state"], "EQUAL")
        self.assertEqual(build_journal_state(100, 200)["state"], "CONFLICT")


class AuthorContentConflictPlanIntegration(unittest.TestCase):
    """Phase 4E, Gap 2 -- build_author_conflict_report() at the plan level."""

    def test_identical_raw_name_no_conflict(self):
        winner = [{"UserID": 97, "AuthorNameRaw": "K Gasmi"}]
        loser = [{"UserID": 97, "AuthorNameRaw": "K Gasmi"}]
        report = build_author_conflict_report(winner, loser)
        self.assertEqual(report["conflicts"], [])
        self.assertTrue(report["execution_permitted"])
        self.assertIsNone(report["blocking_reason"])

    def test_differing_raw_name_is_explicit_conflict_blocking_execution(self):
        winner = [{"UserID": 97, "AuthorNameRaw": "I Ben Ltaifa"}]
        loser = [{"UserID": 97, "AuthorNameRaw": "IB Ltaifa"}]
        report = build_author_conflict_report(winner, loser)
        self.assertEqual(len(report["conflicts"]), 1)
        self.assertFalse(report["execution_permitted"])
        self.assertIsNotNone(report["blocking_reason"])
        self.assertEqual(report["conflicts"][0]["winner_author_name_raw"], "I Ben Ltaifa")
        self.assertEqual(report["conflicts"][0]["loser_author_name_raw"], "IB Ltaifa")

    def test_no_author_rows_is_no_conflict(self):
        report = build_author_conflict_report([], [])
        self.assertEqual(report["conflicts"], [])
        self.assertTrue(report["execution_permitted"])

    def test_phase_4y_genuine_conflict_labeled_and_still_blocks(self):
        """Task F item 17: the new author_conflict_type label must not
        change blocking behavior for a real, non-case-only conflict."""
        winner = [{"UserID": 97, "AuthorNameRaw": "I Ben Ltaifa"}]
        loser = [{"UserID": 97, "AuthorNameRaw": "IB Ltaifa"}]
        report = build_author_conflict_report(winner, loser)
        self.assertFalse(report["execution_permitted"])
        self.assertEqual(report["conflicts"][0]["author_conflict_type"], "GENUINE_CONFLICT")


class CaseOnlyAuthorConflictPlanIntegration(unittest.TestCase):
    """Phase 4Y, Task F items 13-15: real production shapes for the 3
    LOSER_ONLY_BACKFILL candidates, live-verified against production this
    phase (Task G) and reproduced here as fixed fixtures. Policy B
    (CASE_ONLY_ALLOWED_FOR_HUMAN_REVIEW_ONLY): a case-only label must NEVER
    flip execution_permitted to True -- only annotate the reason a human
    reviewer sees."""

    def test_6086_6088_real_shape_labeled_case_only_but_still_blocked(self):
        winner = [{"UserID": 105, "AuthorNameRaw":
                   "A Chakrabarty, N Mansoor, MI Uddin, MH Al-adaileh, N Alsharif, ..."}]
        loser = [{"UserID": 105, "AuthorNameRaw":
                  "A Chakrabarty, N Mansoor, MI Uddin, MH Al-Adaileh, N Alsharif, ..."}]
        report = build_author_conflict_report(winner, loser)
        self.assertEqual(len(report["conflicts"]), 1)
        self.assertFalse(report["execution_permitted"],
                          "Policy B: case-only is a label, never an automatic unblock")
        self.assertEqual(report["conflicts"][0]["author_conflict_type"],
                          "CASE_ONLY_FORMATTING_DIFFERENCE")

    def test_5548_5549_real_shape_remains_blocked_and_genuine(self):
        winner = [{"UserID": 104, "AuthorNameRaw":
                   "S Ahmad, NB Aoun, MA El Affendi, MS Anwar, S Abbas, AA Abd El Latif"}]
        loser = [{"UserID": 104, "AuthorNameRaw":
                  "S Ahmad, N Ben Aoun, MAE Affendi, MS Anwar, S Abbas, AAAE Latif"}]
        report = build_author_conflict_report(winner, loser)
        self.assertEqual(len(report["conflicts"]), 1)
        self.assertFalse(report["execution_permitted"])
        self.assertEqual(report["conflicts"][0]["author_conflict_type"], "GENUINE_CONFLICT")

    def test_6107_6109_real_shape_remains_blocked_and_genuine(self):
        winner = [{"UserID": 69, "AuthorNameRaw": "AM Alomari"}]
        loser = [{"UserID": 69, "AuthorNameRaw": "A Alomari, F Comeau, W Phillips, N Aslam"}]
        report = build_author_conflict_report(winner, loser)
        self.assertEqual(len(report["conflicts"]), 1)
        self.assertFalse(report["execution_permitted"])
        self.assertEqual(report["conflicts"][0]["author_conflict_type"], "GENUINE_CONFLICT")


class CaseOnlyReviewRepresentationTests(unittest.TestCase):
    """Phase 4Z -- pure data contract + pure state machine for representing
    a case-only conflict to a human reviewer. No DB access anywhere in
    this class's fixtures; every plan/conflict dict is hand-built or reused
    from Phase 4Y's real-shape fixtures."""

    def _plan_6086_6088(self):
        winner = [{"UserID": 105, "AuthorNameRaw":
                   "A Chakrabarty, N Mansoor, MI Uddin, MH Al-adaileh, N Alsharif, ..."}]
        loser = [{"UserID": 105, "AuthorNameRaw":
                  "A Chakrabarty, N Mansoor, MI Uddin, MH Al-Adaileh, N Alsharif, ..."}]
        report = build_author_conflict_report(winner, loser)
        plan = {
            "survivor": 6086, "loser": 6088,
            "journal_state": build_journal_state(None, 771),
            "doi_state": build_doi_state(None, None),
            "plan_fingerprint": "fp-test-6086-6088",
            "classification": "PLAN_REQUIRES_HUMAN_APPROVAL",
        }
        return plan, report["conflicts"][0]

    # Task F item 1: 6086/6088 classified as CASE_ONLY_FORMATTING_DIFFERENCE.
    def test_6086_6088_classified_case_only(self):
        _plan, conflict = self._plan_6086_6088()
        self.assertEqual(conflict["author_conflict_type"], "CASE_ONLY_FORMATTING_DIFFERENCE")

    # Task F item 2: classification produces HUMAN_REVIEW_REQUIRED.
    def test_classification_produces_human_review_required(self):
        state, _ = advance_case_only_review(REVIEW_STATE_PLAN_GENERATED)
        self.assertEqual(state, REVIEW_STATE_CASE_ONLY_FORMATTING_DIFFERENCE)
        state2, _ = advance_case_only_review(state)
        self.assertEqual(state2, REVIEW_STATE_HUMAN_REVIEW_REQUIRED)

    def test_approval_reaches_reviewed_approved_state(self):
        state, reason = advance_case_only_review(REVIEW_STATE_HUMAN_REVIEW_REQUIRED, "APPROVE")
        self.assertEqual(state, REVIEW_STATE_REVIEWED_APPROVED)
        self.assertIn("does NOT authorize merge execution", reason)

    # Task F item 4: review rejection blocks further progression.
    def test_rejection_reaches_reviewed_rejected_and_is_terminal(self):
        state, _ = advance_case_only_review(REVIEW_STATE_HUMAN_REVIEW_REQUIRED, "REJECT")
        self.assertEqual(state, REVIEW_STATE_REVIEWED_REJECTED)
        with self.assertRaises(ValueError):
            advance_case_only_review(state, "APPROVE")

    def test_missing_decision_at_human_review_required_raises(self):
        with self.assertRaises(ValueError):
            advance_case_only_review(REVIEW_STATE_HUMAN_REVIEW_REQUIRED)

    def test_approved_state_is_also_terminal(self):
        with self.assertRaises(ValueError):
            advance_case_only_review(REVIEW_STATE_REVIEWED_APPROVED, "APPROVE")

    # Task C data contract.
    def test_review_display_contract_contains_required_fields_and_disclaimer(self):
        plan, conflict = self._plan_6086_6088()
        display = build_case_only_review_display(plan, conflict, tenant_id=1)
        self.assertEqual(display["survivor_paper_id"], 6086)
        self.assertEqual(display["loser_paper_id"], 6088)
        self.assertIn("Al-adaileh", display["survivor_author_name_raw"])
        self.assertIn("Al-Adaileh", display["loser_author_name_raw"])
        self.assertEqual(display["case_only_comparison_result"], "CASE_ONLY_FORMATTING_DIFFERENCE")
        self.assertEqual(display["journal_state"]["state"], "LOSER_ONLY_BACKFILL")
        self.assertEqual(display["tenant_id"], 1)
        self.assertEqual(display["plan_fingerprint"], "fp-test-6086-6088")
        self.assertEqual(display["disclaimer"], CASE_ONLY_REVIEW_DISCLAIMER)
        self.assertIn("does NOT authorize merge execution", display["disclaimer"])

    # Task F item 8: case-only review must not affect 5548/5549 or 6107/6109
    # -- the display builder must refuse a genuine conflict outright.
    def test_5548_5549_genuine_conflict_refused_by_review_display(self):
        winner = [{"UserID": 104, "AuthorNameRaw":
                   "S Ahmad, NB Aoun, MA El Affendi, MS Anwar, S Abbas, AA Abd El Latif"}]
        loser = [{"UserID": 104, "AuthorNameRaw":
                  "S Ahmad, N Ben Aoun, MAE Affendi, MS Anwar, S Abbas, AAAE Latif"}]
        report = build_author_conflict_report(winner, loser)
        plan = {"survivor": 5548, "loser": 5549}
        with self.assertRaises(ValueError):
            build_case_only_review_display(plan, report["conflicts"][0])

    def test_6107_6109_genuine_conflict_refused_by_review_display(self):
        winner = [{"UserID": 69, "AuthorNameRaw": "AM Alomari"}]
        loser = [{"UserID": 69, "AuthorNameRaw": "A Alomari, F Comeau, W Phillips, N Aslam"}]
        report = build_author_conflict_report(winner, loser)
        plan = {"survivor": 6107, "loser": 6109}
        with self.assertRaises(ValueError):
            build_case_only_review_display(plan, report["conflicts"][0])

    # Task F items 5/6/7: purity guard -- this module's Phase 4Z functions
    # must never reference MergeApproval, ResearchPaper writes, or DOI
    # writes. Static source scan, same convention as
    # test_merge_execution_safety.py's NonGoalsStaticSourceScan.
    def test_review_functions_contain_no_db_or_write_vocabulary(self):
        """Source-code-level guard, not merely a docstring claim: no SQL
        verb, no cursor call, no call into execute_approved_merge()/
        merge_group(), and no import of merge_approval/merge_executor
        anywhere in either function's body. (Explanatory prose in the
        docstrings is allowed to mention MergeApproval BY NAME while
        describing what these functions deliberately do NOT do -- that is
        documentation, not a code reference; this check inspects for
        actual executable vocabulary, not every substring.)"""
        import inspect
        import merge_plan_generator as mpg
        src = inspect.getsource(mpg.advance_case_only_review) + inspect.getsource(mpg.build_case_only_review_display)
        forbidden = [
            "UPDATE ", "INSERT ", "DELETE ", "cur.execute", "cur)", "(cur,",
            "execute_approved_merge(", "merge_group(",
            "import merge_approval", "import merge_executor",
        ]
        for token in forbidden:
            self.assertNotIn(token, src, f"Phase 4Z review functions must never contain {token!r}")


class DoiStateCases(unittest.TestCase):
    def test_winner_has_doi_loser_does_not(self):
        state = build_doi_state("10.1/x", None)
        self.assertEqual(state["action"], "KEEP_EXISTING_WINNER_DOI")

    def test_neither_has_doi(self):
        state = build_doi_state(None, None)
        self.assertEqual(state["action"], "NO_CHANGE")

    def test_both_have_same_doi(self):
        state = build_doi_state("10.1/x", "10.1/x")
        self.assertEqual(state["action"], "NO_CHANGE")

    def test_both_have_different_doi_requires_human_review(self):
        state = build_doi_state("10.1/x", "10.1/y")
        self.assertEqual(state["action"], "HUMAN_REVIEW")

    def test_loser_has_doi_winner_does_not_is_anomalous(self):
        state = build_doi_state(None, "10.1/y")
        self.assertEqual(state["action"], "HUMAN_REVIEW")


class ChildTableDependencyPlanning(unittest.TestCase):
    def test_zero_loser_rows_is_no_action(self):
        action = plan_child_action("Citations", "PaperID", "NO ACTION", True,
                                    "citations_greatest", "note", winner_rows=1, loser_rows=0)
        self.assertEqual(action["planned_action"], "NO_ACTION")

    def test_simple_children_remap_with_nonzero_loser_rows(self):
        """At least one child-table dependency with nonzero loser rows,
        as required by Phase 4C task G."""
        action = plan_child_action("ExternalAuthors", "PaperID", "NO ACTION", True,
                                    "simple_children_remap", "note", winner_rows=0, loser_rows=25)
        self.assertEqual(action["planned_action"], "REMAP_TO_SURVIVOR")
        self.assertEqual(action["loser_rows"], 25)
        self.assertIn("remap_simple_child", action["note"] + action["risk"])

    def test_author_review_queue_cascade_is_flagged_review(self):
        action = plan_child_action("AuthorReviewQueue", "PaperID", "CASCADE", False,
                                    "unhandled_cascade", "note", winner_rows=0, loser_rows=3)
        self.assertEqual(action["planned_action"], "REVIEW")
        self.assertIn("CASCADE", action["risk"])

    def test_report_paper_decision_missing_resolved_second_fk_is_review(self):
        action = plan_child_action("ReportPaperDecision", "MissingResolvedToPaperID", "SET NULL",
                                    False, "unhandled_second_fk", "note", winner_rows=0, loser_rows=2)
        self.assertEqual(action["planned_action"], "REVIEW")

    def test_all_specs_have_a_planned_action(self):
        dep_counts = {(t, fk): {"winner": 0, "loser": 5} for t, fk, *_ in CHILD_TABLE_SPECS}
        actions = build_child_table_actions(dep_counts)
        self.assertEqual(len(actions), len(CHILD_TABLE_SPECS))
        for a in actions:
            self.assertIn(a["planned_action"], ("NO_ACTION", "MERGE", "REMAP_TO_SURVIVOR", "REVIEW", "UNKNOWN"))


class ClassificationDecision(unittest.TestCase):
    def test_missing_row_is_blocked(self):
        cls, blockers = compute_classification(None, None, None, [], [], {"action": "NO_CHANGE"}, missing_row=True)
        self.assertEqual(cls, "BLOCKED")

    def test_tenant_mismatch_is_blocked(self):
        cls, blockers = compute_classification("high", None, False, [], [], {"action": "NO_CHANGE"}, tenant_blocked=True)
        self.assertEqual(cls, "BLOCKED")

    def test_year_gap_hard_exclusion_is_blocked(self):
        cls, blockers = compute_classification(
            "review", "different_doi_and_year_gap_gt_2", False, [], [], {"action": "NO_CHANGE"})
        self.assertEqual(cls, "BLOCKED")

    def test_clean_pair_is_safe_plan_candidate(self):
        field_actions = [{"field": "Abstract", "status": STATUS_KEEP_WINNER, "reason": "x"}]
        child_actions = [{"table": "Authors", "foreign_key": "PaperID", "planned_action": "NO_ACTION", "risk": "none"}]
        cls, blockers = compute_classification(
            "high", None, False, field_actions, child_actions, {"action": "KEEP_EXISTING_WINNER_DOI"})
        self.assertEqual(cls, "SAFE_PLAN_CANDIDATE")
        self.assertEqual(blockers, [])

    def test_any_field_conflict_requires_human_approval(self):
        field_actions = [{"field": "PublicationType", "status": STATUS_CONFLICT, "reason": "x"}]
        cls, blockers = compute_classification(
            "high", None, False, field_actions, [], {"action": "KEEP_EXISTING_WINNER_DOI"})
        self.assertEqual(cls, "PLAN_REQUIRES_HUMAN_APPROVAL")
        self.assertTrue(any("PublicationType" in b for b in blockers))


class FullPlanNeverPermitsExecution(unittest.TestCase):
    """Task G: execution_permitted must always be false, on every plan
    shape this module can produce."""

    def _plan(self, **kw):
        winner = rp_row(PaperID=1, DOI="10.1/x", TenantID=1)
        loser = rp_row(PaperID=2, TenantID=1)
        dep_counts = {(t, fk): {"winner": 0, "loser": 0} for t, fk, *_ in CHILD_TABLE_SPECS}
        return generate_pair_plan(1, 2, winner, loser, dep_counts, "high", None, False, "has_doi", **kw)

    def test_safe_candidate_plan_still_not_executable(self):
        plan = self._plan()
        self.assertEqual(plan["classification"], "SAFE_PLAN_CANDIDATE")
        self.assertFalse(plan["execution_permitted"])

    def test_missing_row_plan_not_executable(self):
        plan = generate_pair_plan(1, 2, {}, {}, {}, None, None, None, None, missing_row=True)
        self.assertFalse(plan["execution_permitted"])
        self.assertTrue(plan["requires_human_approval"])

    def test_human_approval_plan_not_executable(self):
        winner = rp_row(PaperID=1, DOI="10.1/x", PublicationType="Conference Paper", TenantID=1)
        loser = rp_row(PaperID=2, PublicationType="Research Article", TenantID=1)
        dep_counts = {(t, fk): {"winner": 0, "loser": 0} for t, fk, *_ in CHILD_TABLE_SPECS}
        plan = generate_pair_plan(1, 2, winner, loser, dep_counts, "high", None, False, "has_doi")
        self.assertEqual(plan["classification"], "PLAN_REQUIRES_HUMAN_APPROVAL")
        self.assertTrue(plan["requires_human_approval"])
        self.assertFalse(plan["execution_permitted"])

    def test_journal_id_conflict_blocks_the_whole_pair(self):
        winner = rp_row(PaperID=1, DOI="10.1/x", JournalID=100, TenantID=1)
        loser = rp_row(PaperID=2, JournalID=200, TenantID=1)
        dep_counts = {(t, fk): {"winner": 0, "loser": 0} for t, fk, *_ in CHILD_TABLE_SPECS}
        plan = generate_pair_plan(1, 2, winner, loser, dep_counts, "high", None, False, "has_doi")
        self.assertEqual(plan["journal_state"]["state"], "CONFLICT")
        self.assertEqual(plan["classification"], "PLAN_REQUIRES_HUMAN_APPROVAL")
        self.assertTrue(plan["requires_human_approval"])
        self.assertFalse(plan["execution_permitted"])
        self.assertTrue(any("journal_state" in b for b in plan["unresolved_conflicts"]))

    def test_author_content_conflict_blocks_the_whole_pair(self):
        winner = rp_row(PaperID=1, DOI="10.1/x", TenantID=1)
        loser = rp_row(PaperID=2, TenantID=1)
        dep_counts = {(t, fk): {"winner": 0, "loser": 0} for t, fk, *_ in CHILD_TABLE_SPECS}
        winner_authors = [{"UserID": 97, "AuthorNameRaw": "I Ben Ltaifa"}]
        loser_authors = [{"UserID": 97, "AuthorNameRaw": "IB Ltaifa"}]
        plan = generate_pair_plan(1, 2, winner, loser, dep_counts, "high", None, False, "has_doi",
                                   winner_authors=winner_authors, loser_authors=loser_authors)
        self.assertEqual(len(plan["author_content_conflicts"]), 1)
        self.assertEqual(plan["classification"], "PLAN_REQUIRES_HUMAN_APPROVAL")
        self.assertFalse(plan["execution_permitted"])
        self.assertTrue(any("author_content_conflicts" in b for b in plan["unresolved_conflicts"]))
        self.assertTrue(any("AuthorNameRaw" in r for r in plan["data_loss_risks"]))

    def test_clean_journal_and_author_state_does_not_block(self):
        winner = rp_row(PaperID=1, DOI="10.1/x", JournalID=440, TenantID=1)
        loser = rp_row(PaperID=2, TenantID=1)
        dep_counts = {(t, fk): {"winner": 0, "loser": 0} for t, fk, *_ in CHILD_TABLE_SPECS}
        winner_authors = [{"UserID": 97, "AuthorNameRaw": "Same Name"}]
        loser_authors = [{"UserID": 97, "AuthorNameRaw": "Same Name"}]
        plan = generate_pair_plan(1, 2, winner, loser, dep_counts, "high", None, False, "has_doi",
                                   winner_authors=winner_authors, loser_authors=loser_authors)
        self.assertEqual(plan["journal_state"]["state"], "WINNER_ONLY")
        self.assertEqual(plan["author_content_conflicts"], [])
        self.assertEqual(plan["classification"], "SAFE_PLAN_CANDIDATE")
        self.assertFalse(plan["execution_permitted"])  # still never true, even when SAFE

    def test_no_execution_hook_exists_in_module_source(self):
        """Static guard: the module must never import dedup_papers.merge_group,
        never define/parse an --apply CLI flag, and every actual cur.execute(...)
        SQL string must be a SELECT (or an information_schema lookup) -- not a
        write. Prose in docstrings/comments explaining what dedup_papers.py's
        real merge_group() does is expected and fine; only real SQL/imports
        matter here."""
        import re
        import merge_plan_generator as mod
        with open(mod.__file__, encoding="utf-8") as f:
            src = f.read()

        import_lines = [l for l in src.splitlines() if l.strip().startswith(("import ", "from "))]
        for line in import_lines:
            self.assertNotIn("merge_group", line, f"merge_group must never be imported: {line!r}")

        self.assertNotIn('add_argument("--apply"', src)
        self.assertNotIn("add_argument('--apply'", src)

        sql_calls = re.findall(r'cur\.execute\(\s*(?:f?)([\'"]{1,3})(.*?)\1', src, re.DOTALL)
        self.assertGreater(len(sql_calls), 0, "expected to find cur.execute(...) calls to check")
        write_verbs = ("INSERT ", "UPDATE ", "DELETE ", "TRUNCATE ", "MERGE ", "ALTER ", "DROP ")
        for _quote, sql in sql_calls:
            sql_upper = sql.upper()
            for verb in write_verbs:
                self.assertNotIn(verb, sql_upper,
                                  f"found a write verb {verb!r} inside an actual cur.execute() SQL string: {sql!r}")


if __name__ == "__main__":
    unittest.main()
