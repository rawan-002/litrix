"""Regression tests for dedup_papers.py's pure decision logic (no DB needed).

Run: cd backend && python tools/test_dedup_papers.py

These exist so a future retune of the fuzzy-matching threshold, the
blocking key, or the confidence heuristic can't silently start
auto-merging "evolutionary papers" -- genuinely different, independently
published works (a conference paper vs. its later journal extension, two
narrow papers by an overlapping team) that can still score >=95% on raw
title similarity. That case was found reviewing a real merge plan
(2026-08-04): two papers with different DOIs, several years apart, whose
titles matched at ~95%+.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dedup_papers import (  # noqa: E402
    author_content_conflicts, choose_keep, choose_keep_reason,
    group_confidence, hard_exclusion_reason, journal_id_decision,
    norm_title, pair_confidence,
    JOURNAL_CONFLICT, JOURNAL_EQUAL, JOURNAL_LOSER_ONLY_BACKFILL,
    JOURNAL_NO_JOURNAL, JOURNAL_WINNER_ONLY,
)


def paper(paper_id, title, doi=None, pub_year=None, citations=0,
          is_verified=True, authors=(1,), first_author=1):
    return {
        "paper_id": paper_id, "title": title,
        "ntitle": "", "doi": doi, "pub_year": pub_year,
        "is_verified": is_verified, "source": "Scholar",
        "citations": citations, "fuzzy_key": norm_title(title),
        "authors": frozenset(authors), "first_author": first_author,
    }


class NizarStyleBoilerplatePrefix(unittest.TestCase):
    """The bug this whole pass started from: PaperID 6086/6088, same
    author, one title carrying a leading 'Research Article ' tag."""

    def test_high_confidence_when_prefix_is_the_only_difference(self):
        papers = {
            6086: paper(6086, "Research Article Prediction Approaches for "
                              "Smart Cultivation: A Comparative Study",
                        doi="10.1155/2021/5534379", pub_year=2021, citations=0),
            6088: paper(6088, "Prediction approaches for smart cultivation: "
                              "a comparative study",
                        doi=None, pub_year=2021, citations=18),
        }
        self.assertEqual(pair_confidence(6086, 6088, papers), "high")
        self.assertEqual(
            group_confidence(6086, [6088], ["fuzzy_title_1.000"], papers), "high")


class EvolutionaryPapersNeverAutoMerge(unittest.TestCase):
    """High text similarity is NOT sufficient on its own -- these are
    independently registered, real, different publications."""

    def test_different_doi_and_year_gap_over_2_is_excluded(self):
        papers = {
            1: paper(1, "Automatic transformation of data warehouse schema "
                        "to NoSQL databases",
                     doi="10.1016/j.procs.2016.08.138", pub_year=2016),
            2: paper(2, "Automatic Transformation of Data Warehouse Schema "
                        "to NoSQL Graph databases",
                     doi="10.1016/j.procs.2025.09.455", pub_year=2025),
        }
        self.assertEqual(
            hard_exclusion_reason(1, 2, papers), "different_doi_and_year_gap_gt_2")
        self.assertEqual(pair_confidence(1, 2, papers), "review")

    def test_different_doi_and_different_first_author_is_excluded(self):
        papers = {
            1: paper(1, "Model Based Testing for Real-Time Systems",
                     doi="10.1007/978-3-319-40180-5_19", pub_year=2016,
                     authors=(1, 2), first_author=1),
            2: paper(2, "Model-based testing of reactive systems",
                     doi="10.1007/b137241", pub_year=2017,
                     authors=(2, 3), first_author=2),
        }
        self.assertEqual(
            hard_exclusion_reason(1, 2, papers),
            "different_doi_and_different_first_author")
        self.assertEqual(pair_confidence(1, 2, papers), "review")

    def test_same_first_author_and_year_gap_of_1_is_not_excluded_by_hard_rule(self):
        """The hard-exclusion rules are narrow on purpose -- they fire on
        year_gap>2 or a differing first author, not on 'any DOI mismatch'.
        A genuine near-duplicate with two real DOIs a year apart (rare, but
        possible for a preprint vs VoR) still isn't [HIGH] though, because
        pair_confidence's own both-have-a-DOI check catches it separately."""
        papers = {
            1: paper(1, "Same paper preprint then published version",
                     doi="10.1000/preprint", pub_year=2020, first_author=1),
            2: paper(2, "Same paper preprint then published version",
                     doi="10.1000/published", pub_year=2021, first_author=1),
        }
        self.assertIsNone(hard_exclusion_reason(1, 2, papers))
        # Still not [HIGH]: both sides carry a (different) DOI, which isn't
        # the asymmetric "one enriched, one not yet" signal pair_confidence
        # trusts for auto-merge.
        self.assertEqual(pair_confidence(1, 2, papers), "review")


class DistinctRecordTypesNeverAutoMerge(unittest.TestCase):
    def test_corrigendum_is_never_high_even_with_perfect_signals(self):
        papers = {
            1: paper(1, "Optimized recurrent neural network mechanism for "
                        "olive leaf disease diagnosis",
                     doi="10.1016/j.aej.2023.07.037", pub_year=2023),
            2: paper(2, "Corrigendum to “Optimized recurrent neural "
                        "network mechanism for olive leaf disease "
                        "diagnosis”",
                     doi=None, pub_year=2023),
        }
        self.assertEqual(pair_confidence(1, 2, papers), "review")


class NoSharedAuthorNeverAutoMerge(unittest.TestCase):
    def test_similar_title_different_authors_is_excluded(self):
        papers = {
            1: paper(1, "Acknowledgment to the Reviewers of Sensors in 2022",
                     doi=None, pub_year=2022, authors=(1,), first_author=1),
            2: paper(2, "Acknowledgment to Reviewers of Sensors in 2021",
                     doi=None, pub_year=2021, authors=(2,), first_author=2),
        }
        self.assertEqual(pair_confidence(1, 2, papers), "review")


class ChooseKeepReasonReporting(unittest.TestCase):
    def test_has_doi_is_the_decisive_factor(self):
        papers = {
            1: paper(1, "X", doi="10.1/x", citations=0),
            2: paper(2, "X", doi=None, citations=100),
        }
        keep, losers = choose_keep([1, 2], papers)
        self.assertEqual(keep, 1)
        self.assertEqual(choose_keep_reason(keep, losers, papers), "has_doi")

    def test_citations_decide_when_doi_presence_ties(self):
        papers = {
            1: paper(1, "X", doi="10.1/a", citations=5),
            2: paper(2, "X", doi="10.1/b", citations=50),
        }
        keep, losers = choose_keep([1, 2], papers)
        self.assertEqual(keep, 2)
        self.assertEqual(choose_keep_reason(keep, losers, papers), "citations")


class JournalIdDecisionModel(unittest.TestCase):
    """Phase 4E, Gap 1: merge_group() never touches JournalID today, so a
    populated loser value can be silently discarded. journal_id_decision()
    only plans what a future executor would need to do -- it never writes."""

    def test_both_null_is_no_journal(self):
        state, action = journal_id_decision(None, None)
        self.assertEqual(state, JOURNAL_NO_JOURNAL)

    def test_winner_only(self):
        state, action = journal_id_decision(440, None)
        self.assertEqual(state, JOURNAL_WINNER_ONLY)
        self.assertIn("no action", action)

    def test_loser_only_is_explicit_backfill_plan(self):
        state, action = journal_id_decision(None, 676)
        self.assertEqual(state, JOURNAL_LOSER_ONLY_BACKFILL)
        self.assertIn("backfill", action.lower())

    def test_equal_values(self):
        state, action = journal_id_decision(676, 676)
        self.assertEqual(state, JOURNAL_EQUAL)

    def test_conflicting_values_is_conflict_not_a_silent_choice(self):
        state, action = journal_id_decision(100, 200)
        self.assertEqual(state, JOURNAL_CONFLICT)
        self.assertIn("do not silently choose one", action)


class AuthorContentConflictDetection(unittest.TestCase):
    """Phase 4E, Gap 2: merge_group()'s Authors remap is
    INSERT ... ON CONFLICT (UserID,PaperID) DO NOTHING -- a shared UserID
    with a differing AuthorNameRaw is silently discarded with zero
    detection today. author_content_conflicts() must catch exactly that,
    using the same (UserID,PaperID) identity the SQL already keys on --
    no fuzzy matching, no normalization."""

    def test_identical_raw_name_is_no_conflict(self):
        winner = [{"UserID": 97, "AuthorNameRaw": "K Gasmi, M Krichen"}]
        loser = [{"UserID": 97, "AuthorNameRaw": "K Gasmi, M Krichen"}]
        self.assertEqual(author_content_conflicts(winner, loser), [])

    def test_different_raw_formatting_is_an_explicit_conflict(self):
        winner = [{"UserID": 97, "AuthorNameRaw": "I Ben Ltaifa"}]
        loser = [{"UserID": 97, "AuthorNameRaw": "IB Ltaifa"}]
        conflicts = author_content_conflicts(winner, loser)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["UserID"], 97)
        self.assertEqual(conflicts[0]["winner_author_name_raw"], "I Ben Ltaifa")
        self.assertEqual(conflicts[0]["loser_author_name_raw"], "IB Ltaifa")

    def test_only_the_actually_conflicting_row_is_reported(self):
        """Multiple shared authors, only one has a differing raw name --
        the non-conflicting UserID must not appear in the result."""
        winner = [
            {"UserID": 6, "AuthorNameRaw": "A Chakrabarty"},
            {"UserID": 105, "AuthorNameRaw": "MH Al-adaileh"},
        ]
        loser = [
            {"UserID": 6, "AuthorNameRaw": "A Chakrabarty"},
            {"UserID": 105, "AuthorNameRaw": "MH Al-Adaileh"},
        ]
        conflicts = author_content_conflicts(winner, loser)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["UserID"], 105)

    def test_no_shared_uid_is_no_false_conflict(self):
        winner = [{"UserID": 1, "AuthorNameRaw": "A One"}]
        loser = [{"UserID": 2, "AuthorNameRaw": "B Two"}]
        self.assertEqual(author_content_conflicts(winner, loser), [])

    def test_conflict_is_not_hidden_by_the_real_merge_paths_on_conflict_do_nothing(self):
        """The real SQL (INSERT ... ON CONFLICT (UserID,PaperID) DO
        NOTHING) would silently keep the winner's row and never even look
        at the loser's AuthorNameRaw. This function must surface the
        collision explicitly rather than reproducing that silence."""
        winner = [{"UserID": 104, "AuthorNameRaw": "S Ahmad, NB Aoun"}]
        loser = [{"UserID": 104, "AuthorNameRaw": "S Ahmad, N Ben Aoun"}]
        conflicts = author_content_conflicts(winner, loser)
        self.assertEqual(len(conflicts), 1, "ON CONFLICT DO NOTHING silence must not propagate into detection")
        self.assertEqual(conflicts[0]["loser_author_name_raw"], "S Ahmad, N Ben Aoun")


if __name__ == "__main__":
    unittest.main()
