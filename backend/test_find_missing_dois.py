"""Regression tests for find_missing_dois.py's Phase 3B doi_pipeline
integration -- retrieval-outcome classification, PaperInput/CandidateInput
construction, and the resolve() glue (sources_attempted wiring).

Run: cd backend && python test_find_missing_dois.py

No DB, no network. requests.get and openalex_get are mocked. These exist
so the PaperID 7545 safety invariant (a source that was attempted and
failed must never be scored as if it had never been queried) stays true
at the orchestration layer, not just inside doi_pipeline.resolver itself.
"""
import os
import sys
import unittest
from unittest.mock import patch

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from find_missing_dois import (  # noqa: E402
    OUTCOME_ERROR, OUTCOME_FAILED, OUTCOME_NO_RESULTS, OUTCOME_RATE_LIMITED,
    OUTCOME_SUCCEEDED, OUTCOME_TIMEOUT,
    build_paper_input, crossref_candidate, doi_taken, openalex_candidate,
    resolve_for_paper, split_authors,
)
from doi_pipeline import AUTO_MATCH, REJECT, REVIEW, CandidateInput, PaperInput, resolve  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json_data = json_data or {}

    def json(self):
        return self._json_data


CROSSREF_HIT = {
    'message': {
        'items': [{
            'DOI': '10.1109/access.2024.1234567',
            'title': ['Deep Learning for Intrusion Detection in SCADA Networks'],
            'author': [{'given': 'Ali', 'family': 'Alzahrani'},
                       {'given': 'John', 'family': 'Smith'}],
            'issued': {'date-parts': [[2024]]},
            'container-title': ['IEEE Access'],
            'publisher': 'IEEE',
            'type': 'journal-article',
            'volume': '12', 'issue': '3', 'page': '100-110',
        }]
    }
}

OPENALEX_HIT = {
    'results': [{
        'doi': 'https://doi.org/10.1109/access.2024.1234567',
        'title': 'Deep Learning for Intrusion Detection in SCADA Networks',
        'authorships': [{'author': {'display_name': 'Ali Alzahrani'}},
                         {'author': {'display_name': 'John Smith'}}],
        'publication_year': 2024,
        'primary_location': {'source': {'display_name': 'IEEE Access',
                                         'host_organization_name': 'IEEE'}},
        'type': 'article',
        'biblio': {'volume': '12', 'issue': '3', 'first_page': '100', 'last_page': '110'},
    }]
}

OUR_PAPER = PaperInput(
    title='Deep Learning for Intrusion Detection in SCADA Networks',
    authors=['Ali Alzahrani', 'John Smith'], year=2024, venue='IEEE Access',
)


# -----------------------------------------------------------------------------
# 1/2. JSON -> CandidateInput conversion
# -----------------------------------------------------------------------------

class ConversionTests(unittest.TestCase):
    @patch('find_missing_dois.requests.get')
    def test_crossref_json_to_candidate(self, mock_get):
        mock_get.return_value = _FakeResponse(200, CROSSREF_HIT)
        cand, outcome = crossref_candidate(OUR_PAPER.title)
        self.assertEqual(outcome, OUTCOME_SUCCEEDED)
        self.assertIsInstance(cand, CandidateInput)
        self.assertEqual(cand.source, 'crossref')
        self.assertEqual(cand.doi, '10.1109/access.2024.1234567')
        self.assertEqual(cand.title, 'Deep Learning for Intrusion Detection in SCADA Networks')
        self.assertEqual(cand.authors, ['Ali Alzahrani', 'John Smith'])
        self.assertEqual(cand.year, 2024)
        self.assertEqual(cand.venue, 'IEEE Access')
        self.assertEqual(cand.doc_type, 'journal-article')
        self.assertEqual(cand.volume, '12')
        self.assertEqual(cand.issue, '3')
        self.assertEqual(cand.pages, '100-110')

    @patch('find_missing_dois.openalex_get')
    def test_openalex_json_to_candidate(self, mock_oa):
        mock_oa.return_value = OPENALEX_HIT
        cand, outcome = openalex_candidate(OUR_PAPER.title)
        self.assertEqual(outcome, OUTCOME_SUCCEEDED)
        self.assertIsInstance(cand, CandidateInput)
        self.assertEqual(cand.source, 'openalex')
        self.assertEqual(cand.doi, '10.1109/access.2024.1234567')  # wrapper stripped
        self.assertEqual(cand.authors, ['Ali Alzahrani', 'John Smith'])
        self.assertEqual(cand.year, 2024)
        self.assertEqual(cand.venue, 'IEEE Access')
        self.assertEqual(cand.doc_type, 'article')
        self.assertEqual(cand.pages, '100-110')


# -----------------------------------------------------------------------------
# 3/4. no_results vs failed must not collapse
# -----------------------------------------------------------------------------

class OutcomeClassificationTests(unittest.TestCase):
    @patch('find_missing_dois.requests.get')
    def test_crossref_no_results(self, mock_get):
        mock_get.return_value = _FakeResponse(200, {'message': {'items': []}})
        cand, outcome = crossref_candidate('some obscure title')
        self.assertIsNone(cand)
        self.assertEqual(outcome, OUTCOME_NO_RESULTS)

    @patch('find_missing_dois.openalex_get')
    def test_openalex_no_results(self, mock_oa):
        mock_oa.return_value = {'results': []}
        cand, outcome = openalex_candidate('some obscure title')
        self.assertIsNone(cand)
        self.assertEqual(outcome, OUTCOME_NO_RESULTS)

    @patch('find_missing_dois.requests.get')
    def test_crossref_timeout_distinct_from_no_results(self, mock_get):
        mock_get.side_effect = requests.Timeout('simulated timeout')
        cand, outcome = crossref_candidate('x')
        self.assertIsNone(cand)
        self.assertEqual(outcome, OUTCOME_TIMEOUT)
        self.assertNotEqual(outcome, OUTCOME_NO_RESULTS)

    @patch('find_missing_dois.requests.get')
    def test_crossref_rate_limited_distinct_from_no_results(self, mock_get):
        mock_get.return_value = _FakeResponse(429, {})
        cand, outcome = crossref_candidate('x')
        self.assertIsNone(cand)
        self.assertEqual(outcome, OUTCOME_RATE_LIMITED)
        self.assertNotEqual(outcome, OUTCOME_NO_RESULTS)

    @patch('find_missing_dois.requests.get')
    def test_crossref_generic_error_distinct_from_no_results(self, mock_get):
        mock_get.return_value = _FakeResponse(500, {})
        cand, outcome = crossref_candidate('x')
        self.assertIsNone(cand)
        self.assertEqual(outcome, OUTCOME_ERROR)
        self.assertNotEqual(outcome, OUTCOME_NO_RESULTS)

    @patch('find_missing_dois.openalex_get')
    def test_openalex_failed_distinct_from_no_results(self, mock_oa):
        # openalex_get() itself returns bare None on any failure (timeout,
        # exhausted-retry 429, other HTTP error, network exception) -- see
        # the module-level note in find_missing_dois.py on why this is
        # coarser than Crossref's outcome set (out-of-scope to widen
        # scrapers/orcid.py in this phase).
        mock_oa.return_value = None
        cand, outcome = openalex_candidate('x')
        self.assertIsNone(cand)
        self.assertEqual(outcome, OUTCOME_FAILED)
        self.assertNotEqual(outcome, OUTCOME_NO_RESULTS)


# -----------------------------------------------------------------------------
# 5/6/7/8. sources_attempted wiring + the PaperID 7545 invariant
# -----------------------------------------------------------------------------

class SourcesAttemptedTests(unittest.TestCase):
    def _perfect_candidate(self, source):
        return CandidateInput(
            source=source, doi='10.1109/access.2024.1234567',
            title=OUR_PAPER.title, authors=list(OUR_PAPER.authors),
            year=2024, venue='IEEE Access',
        )

    def test_5_sources_attempted_counts_attempts_not_successes(self):
        # Both sources FAILED outright (no candidates at all) -- attempts
        # were still made, so sources_attempted must be 2, not 0.
        result, sources_attempted, candidates = resolve_for_paper(
            OUR_PAPER, None, OUTCOME_TIMEOUT, None, OUTCOME_FAILED, {})
        self.assertEqual(sources_attempted, 2)
        self.assertEqual(candidates, [])
        self.assertEqual(result.decision, REJECT)  # no_candidates

    def test_6_both_sources_succeed_and_agree_auto_matches(self):
        cr = self._perfect_candidate('crossref')
        oa = self._perfect_candidate('openalex')
        result, sources_attempted, candidates = resolve_for_paper(
            OUR_PAPER, cr, OUTCOME_SUCCEEDED, oa, OUTCOME_SUCCEEDED, {})
        self.assertEqual(sources_attempted, 2)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(result.source_agreement, 'agree')
        self.assertEqual(result.decision, AUTO_MATCH)

    def test_7_paperid_7545_equivalent_must_not_auto_match(self):
        # Crossref returns a perfect, high-scoring candidate; OpenAlex was
        # attempted but failed (429/timeout/etc, collapsed to 'failed').
        # sources_attempted=2 > len(present)=1 -> single_source_after_failure
        # -> capped at REVIEW even though the surviving candidate scores 1.0.
        cr = self._perfect_candidate('crossref')
        result, sources_attempted, candidates = resolve_for_paper(
            OUR_PAPER, cr, OUTCOME_SUCCEEDED, None, OUTCOME_FAILED, {})
        self.assertEqual(sources_attempted, 2)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(result.source_agreement, 'single_source_after_failure')
        self.assertNotEqual(result.decision, AUTO_MATCH)
        self.assertEqual(result.decision, REVIEW)

    def test_8_existing_single_source_by_design_behavior_unchanged(self):
        # Calling doi_pipeline.resolve() directly the OLD way (no
        # sources_attempted, exactly as benchmark.py/test_resolver.py do)
        # must still reach AUTO_MATCH on a single strong candidate --
        # resolver.py itself is untouched by this integration.
        cr = self._perfect_candidate('crossref')
        result = resolve(OUR_PAPER, [cr], known_dois={})
        self.assertEqual(result.source_agreement, 'single_source')
        self.assertEqual(result.decision, AUTO_MATCH)


# -----------------------------------------------------------------------------
# 9/10. REVIEW/REJECT must never look like AUTO_MATCH
# -----------------------------------------------------------------------------

class WriteEligibilityTests(unittest.TestCase):
    def test_9_review_never_equals_auto_match(self):
        cr = CandidateInput(
            source='crossref', doi='10.1109/access.2024.1234567',
            title=OUR_PAPER.title, authors=list(OUR_PAPER.authors),
            year=2024, venue='IEEE Access',
        )
        result, _, _ = resolve_for_paper(
            OUR_PAPER, cr, OUTCOME_SUCCEEDED, None, OUTCOME_FAILED, {})
        self.assertEqual(result.decision, REVIEW)
        self.assertNotEqual(result.decision, AUTO_MATCH)

    def test_10_reject_never_equals_auto_match(self):
        unrelated = CandidateInput(
            source='crossref', doi='10.1109/xyz.2024.999',
            title='Completely Unrelated Paper About Something Else',
            authors=['Zaid Othman'], year=2024,
        )
        result, _, _ = resolve_for_paper(
            OUR_PAPER, unrelated, OUTCOME_SUCCEEDED, None, OUTCOME_NO_RESULTS, {})
        self.assertEqual(result.decision, REJECT)
        self.assertNotEqual(result.decision, AUTO_MATCH)


# -----------------------------------------------------------------------------
# 11. known_dois (resolver-time) and doi_taken() (live) are additive, not
#     interchangeable
# -----------------------------------------------------------------------------

class DuplicateProtectionTests(unittest.TestCase):
    def test_known_dois_resolver_gate_rejects_duplicate(self):
        cr = CandidateInput(
            source='crossref', doi='10.1109/access.2024.1234567',
            title=OUR_PAPER.title, authors=list(OUR_PAPER.authors),
            year=2024, venue='IEEE Access',
        )
        oa = CandidateInput(
            source='openalex', doi='10.1109/access.2024.1234567',
            title=OUR_PAPER.title, authors=list(OUR_PAPER.authors),
            year=2024, venue='IEEE Access',
        )
        known_dois = {'10.1109/access.2024.1234567': 999}
        result, _, _ = resolve_for_paper(
            OUR_PAPER, cr, OUTCOME_SUCCEEDED, oa, OUTCOME_SUCCEEDED, known_dois)
        self.assertEqual(result.decision, REJECT)
        self.assertTrue(any('duplicate_doi_owned_by_paper_999' in r for r in result.reasons))

    def test_doi_taken_is_a_separate_live_mechanism_not_the_same_as_known_dois(self):
        # Structural check only (no DB): doi_taken() requires a live
        # connection + a specific paper_id to exclude, which known_dois
        # (a plain in-memory dict populated once at the start of a run)
        # cannot express -- confirms the two checks are independent
        # mechanisms, not one replacing the other.
        varnames = doi_taken.__code__.co_varnames[:doi_taken.__code__.co_argcount]
        self.assertEqual(varnames, ('conn', 'doi', 'paper_id'))


# -----------------------------------------------------------------------------
# split_authors / build_paper_input -- small pure helpers
# -----------------------------------------------------------------------------

class HelperTests(unittest.TestCase):
    def test_split_authors_pipe_joined(self):
        self.assertEqual(split_authors('Ali Alzahrani | John Smith'),
                          ['Ali Alzahrani', 'John Smith'])

    def test_split_authors_empty(self):
        self.assertEqual(split_authors(None), [])
        self.assertEqual(split_authors(''), [])

    def test_build_paper_input_maps_widened_fields(self):
        row = {
            'PaperID': 1, 'Title': 'X', 'PubYear': 2023,
            'Volume': '5', 'Issue': '2', 'Pages': '1-10',
            'PublicationType': 'Research Article', 'JournalName': 'Some Journal',
            'AuthorNameRaw': 'Ali Alzahrani | John Smith',
        }
        p = build_paper_input(row)
        self.assertIsInstance(p, PaperInput)
        self.assertEqual(p.title, 'X')
        self.assertEqual(p.year, 2023)
        self.assertEqual(p.volume, '5')
        self.assertEqual(p.issue, '2')
        self.assertEqual(p.pages, '1-10')
        self.assertEqual(p.publication_type, 'Research Article')
        self.assertEqual(p.venue, 'Some Journal')
        self.assertEqual(p.authors, ['Ali Alzahrani', 'John Smith'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
