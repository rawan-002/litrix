"""Unit tests for doi_pipeline/resolver.py -- pure functions, no I/O.

Includes the 8 mandatory regression cases from the Phase 2 DOI-pipeline
audit (static fixtures, no network/DB access -- see benchmark.py for the
same cases with full reporting output).

Run directly:
    python backend/doi_pipeline/tests/test_resolver.py
Or via pytest (if installed):
    python -m pytest backend/doi_pipeline/tests -q
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # doi_pipeline/

from resolver import (  # noqa: E402
    AUTO_MATCH,
    AUTO_MATCH_THRESHOLD,
    REJECT,
    REVIEW,
    CandidateInput,
    PaperInput,
    author_signal,
    check_duplicate_doi,
    detect_special_type,
    publication_type_conflict,
    resolve,
    source_agreement_state,
    year_score,
)


class AuthorSignalTests(unittest.TestCase):
    def test_missing_evidence_both_sides_none(self):
        fa, ao = author_signal((), ())
        self.assertIsNone(fa)
        self.assertIsNone(ao)

    def test_missing_evidence_one_side_empty_is_still_none(self):
        """Missing evidence must never be scored as a mismatch -- this is
        the exact situation PaperID 5645 was in (Authors.AuthorNameRaw
        empty for that row)."""
        fa, ao = author_signal((), ['Alaka Ananth', 'K. Chandrasekaran'])
        self.assertIsNone(fa)
        self.assertIsNone(ao)

    def test_zero_overlap_when_both_sides_present(self):
        fa, ao = author_signal(['Saad Alqithami'], ['Alaka Ananth', 'K. Chandrasekaran'])
        self.assertEqual(fa, 0.0)
        self.assertEqual(ao, 0.0)

    def test_full_overlap_single_author(self):
        fa, ao = author_signal(['Saad Alqithami'], ['Saad Alqithami'])
        self.assertEqual(fa, 1.0)
        self.assertEqual(ao, 1.0)

    def test_partial_overlap(self):
        fa, ao = author_signal(
            ['Saad Alqithami', 'Someone Else'],
            ['Mustafa Khaleel', 'Saad Alqithami', 'Michelle Zhu'],
        )
        # first author "Alqithami" is NOT first in the candidate list, but
        # IS present somewhere in it -> counted as first_author_match=1.0
        # (per author_signal's docstring: presence, not position, of our
        # first author in the candidate set).
        self.assertEqual(fa, 1.0)
        self.assertAlmostEqual(ao, 0.5)  # 1 of our 2 surnames found


class YearScoreTests(unittest.TestCase):
    def test_missing_year_is_none_not_zero(self):
        self.assertIsNone(year_score(None, 2020))
        self.assertIsNone(year_score(2020, None))

    def test_exact_match(self):
        self.assertEqual(year_score(2020, 2020), 1.0)

    def test_one_year_gap(self):
        self.assertEqual(year_score(2020, 2021), 0.6)
        self.assertEqual(year_score(2021, 2020), 0.6)

    def test_gap_over_one_is_zero(self):
        self.assertEqual(year_score(2013, 2015), 0.0)


class DuplicateDoiTests(unittest.TestCase):
    def test_unknown_doi_returns_none(self):
        self.assertIsNone(check_duplicate_doi('10.1109/abc', {}))

    def test_known_doi_returns_owner(self):
        owner = check_duplicate_doi(
            'https://doi.org/10.1109/ABC', {'10.1109/abc': 4242})
        self.assertEqual(owner, 4242)

    def test_malformed_doi_returns_none(self):
        self.assertIsNone(check_duplicate_doi('not-a-doi', {'x': 1}))

    def test_pure_no_db_access(self):
        """This is a static assertion, not a runtime one: the function
        signature only accepts an in-memory Mapping, and the module has no
        DB/network IMPORT statements (checked for actual `import`/`from`
        lines, not bare substrings -- resolver.py's own docstring mentions
        "requests/httpx" in prose, which a naive substring check would
        wrongly flag)."""
        import resolver
        with open(resolver.__file__, encoding='utf-8') as f:
            src = f.read()
        for forbidden in ('import psycopg2', 'import django', 'from django',
                           'import requests', 'import httpx'):
            self.assertNotIn(forbidden, src)


class PublicationTypeConflictTests(unittest.TestCase):
    def test_research_article_vs_corrigendum_conflicts(self):
        paper = PaperInput(title='Optimized RNN for leaf disease',
                            publication_type='Research Article')
        cand = CandidateInput(source='crossref', doi='10.1/x',
                               title='Corrigendum to "Optimized RNN for leaf disease"')
        self.assertIsNotNone(publication_type_conflict(paper, cand))

    def test_plain_vs_plain_no_conflict(self):
        paper = PaperInput(title='A Study of X', publication_type='Research Article')
        cand = CandidateInput(source='crossref', doi='10.1/x', title='A Study of X')
        self.assertIsNone(publication_type_conflict(paper, cand))

    def test_both_corrections_no_conflict(self):
        paper = PaperInput(title='Correction to: something', publication_type='Correction')
        cand = CandidateInput(source='crossref', doi='10.1/x', title='Correction to: something')
        self.assertIsNone(publication_type_conflict(paper, cand))


class ThesisDissertationGateTests(unittest.TestCase):
    """Phase 2C. See resolver.py's _THESIS_DISSERTATION_PATTERNS docstring
    for the word-boundary rationale and the documented PaperID 5241/5439
    scope decision (this gate protects titles that explicitly say "thesis"
    or "dissertation"; it does NOT retroactively protect 5241/5439 itself,
    whose title says "Contributions to..." with neither word -- that is an
    accepted, documented residual risk, not a bug)."""

    # -- detect_special_type(): unit-level checks -----------------------

    def test_detects_thesis_anywhere_in_title(self):
        self.assertEqual(detect_special_type('Distributed Systems: A PhD Thesis'), 'thesis')
        self.assertEqual(detect_special_type('A Thesis on Distributed Systems'), 'thesis')

    def test_detects_dissertation_anywhere_in_title(self):
        self.assertEqual(
            detect_special_type('A Dissertation on Model-Based Testing'), 'dissertation')
        self.assertEqual(
            detect_special_type('Model-Based Testing (Doctoral Dissertation)'), 'dissertation')

    def test_word_boundary_does_not_misfire_on_similar_words(self):
        """The exact false-positive trap this gate must avoid: 'thesis' is
        a literal substring of these ordinary scientific words."""
        for title in (
            'Synthesis of Novel Nanomaterials for Drug Delivery',
            'A Hypothesis-Driven Approach to Cancer Research',
            'Parenthesis Matching in Compiler Design',
            'Antithesis and Synthesis in Hegelian Philosophy',
        ):
            self.assertIsNone(detect_special_type(title), msg=f'title={title!r}')

    def test_contributions_to_alone_is_not_special_type(self):
        """Required scenario C: ambiguous phrasing must not trigger the gate
        by itself -- this is the exact PaperID 5241 title text."""
        self.assertIsNone(detect_special_type(
            'Contributions to model-based testing of dynamic and '
            'distributed real-time systems'
        ))

    def test_case_variations_handled_consistently(self):
        """Required scenario D."""
        for variant in ('THESIS', 'Thesis', 'thesis', 'ThEsIs'):
            self.assertEqual(
                detect_special_type(f'Distributed Systems: A {variant}'), 'thesis',
                msg=f'variant={variant!r}')
        for variant in ('DISSERTATION', 'Dissertation', 'dissertation'):
            self.assertEqual(
                detect_special_type(f'A {variant} on Distributed Systems'), 'dissertation',
                msg=f'variant={variant!r}')

    # -- publication_type_conflict(): gate-level checks ------------------

    def test_thesis_vs_plain_paper_conflicts(self):
        paper = PaperInput(title='Distributed Systems: A PhD Thesis')
        cand = CandidateInput(source='crossref', doi='10.1109/x',
                               title='Distributed Systems')
        self.assertIsNotNone(publication_type_conflict(paper, cand))

    def test_plain_paper_vs_thesis_conflicts_symmetrically(self):
        paper = PaperInput(title='Distributed Systems')
        cand = CandidateInput(source='crossref', doi='10.1109/x',
                               title='Distributed Systems: A PhD Thesis')
        self.assertIsNotNone(publication_type_conflict(paper, cand))

    def test_both_theses_no_conflict(self):
        paper = PaperInput(title='Distributed Systems: A PhD Thesis')
        cand = CandidateInput(source='crossref', doi='10.1109/x',
                               title='Distributed Systems: A PhD Thesis')
        self.assertIsNone(publication_type_conflict(paper, cand))

    def test_contributions_to_paper_vs_paper_no_conflict(self):
        """Required scenario C, at the gate level: ordinary papers using
        the word 'contributions' must match normally."""
        paper = PaperInput(title='Contributions to model-based testing of '
                                  'dynamic and distributed real-time systems')
        cand = CandidateInput(source='crossref', doi='10.1109/x',
                               title='Contributions to model-based testing of '
                                     'dynamic and distributed real-time systems')
        self.assertIsNone(publication_type_conflict(paper, cand))

    # -- full resolve() integration: required scenarios A and B ---------

    def test_A_thesis_vs_similar_paper_must_not_auto_match(self):
        """Required scenario A. A highly similar title/author/year pair
        where OUR side is explicitly a thesis must be blocked from
        AUTO_MATCH by the hard gate, regardless of how high every other
        signal scores."""
        paper = PaperInput(
            title='Model-Based Testing of Dynamic and Distributed Real-Time '
                  'Systems: A PhD Thesis',
            authors=['Saad Alqithami'], year=2018,
        )
        cand = CandidateInput(
            source='crossref', doi='10.1109/icrito.2017.8342406',
            title='Model-Based Testing of Dynamic and Distributed Real-Time Systems',
            authors=['Saad Alqithami'], year=2018,
        )
        result = resolve(paper, [cand], known_dois={})
        self.assertEqual(result.decision, REJECT)
        self.assertTrue(any('type_conflict' in r for r in result.reasons))
        # Note: result.doi is still populated with the rejected candidate's
        # DOI here by design (see resolve()'s gate-failure branch) -- REJECT
        # results carry the evidence of what was rejected and why, for
        # provenance/audit purposes. The safety property under test is
        # `decision != AUTO_MATCH`, since only AUTO_MATCH is ever
        # write-eligible per the Phase 2A contract -- not that `.doi` is None.
        self.assertNotEqual(result.decision, AUTO_MATCH)

    def test_B_ordinary_paper_to_paper_matching_still_works(self):
        """Required scenario B: this gate must not affect ordinary
        (non-thesis) high-confidence matches -- reuses the same
        PaperID-3015-shaped fixture pattern from the n=8 benchmark."""
        paper = PaperInput(
            title='The Feasibility of Using Random Administrative Samples '
                  '(The French Experiment) in Population Censuses in '
                  'African Countries',
            year=2023,
        )
        cand = CandidateInput(
            source='stored_doi_revalidation', doi='10.47191/ijcsrr/V6-i2-93',
            title='The Feasibility of Using Random Administrative Samples '
                  '(The French Experiment) in Population Censuses in '
                  'African Countries',
            authors=['Elkhatim Abuelysar Elmobarak'], year=2023,
        )
        result = resolve(paper, [cand], known_dois={})
        self.assertEqual(result.decision, AUTO_MATCH)

    def test_5241_5439_boundary_case_documented_scope(self):
        """Documents, rather than silently assumes, the Phase 2C scope
        decision: this exact real pair (title says 'Contributions to...',
        never 'thesis'/'dissertation') is NOT caught by the new gate --
        per the brief, 'contributions to' alone is too ambiguous to gate
        on. It is caught in practice by duplicate-DOI protection (Phase
        2B finding: PaperID 5439's DOI is already owned by PaperID 5439
        in the real DB), which is exercised separately, not by this gate."""
        paper = PaperInput(
            title='Contributions to model-based testing of dynamic and '
                  'distributed real-time systems',
            year=2018,
        )
        cand = CandidateInput(
            source='crossref', doi='10.1109/icrito.2017.8342406',
            title='Model-Based Testing of Dynamic and Distributed Real-Time Systems',
            year=2018,
        )
        self.assertIsNone(publication_type_conflict(paper, cand))
        # Without duplicate-DOI protection this specific fixture would
        # score into AUTO_MATCH range -- documenting the known gap, not
        # asserting it is safe on its own.
        result = resolve(paper, [cand], known_dois={})
        self.assertEqual(result.decision, AUTO_MATCH)
        # ...but WITH the real, already-owned DOI supplied (as any real
        # Phase 2D/2F run would do), duplicate protection catches it:
        result_protected = resolve(
            paper, [cand], known_dois={'10.1109/icrito.2017.8342406': 5439})
        self.assertEqual(result_protected.decision, REJECT)
        self.assertTrue(any('duplicate_doi' in r for r in result_protected.reasons))


class SourceAgreementStateTests(unittest.TestCase):
    def test_no_candidates(self):
        self.assertEqual(source_agreement_state([]), 'no_candidates')

    def test_single_source(self):
        c = CandidateInput(source='crossref', doi='10.1/x', title='T')
        self.assertEqual(source_agreement_state([c]), 'single_source')

    def test_agree(self):
        c1 = CandidateInput(source='crossref', doi='10.1109/x', title='T')
        c2 = CandidateInput(source='openalex', doi='https://doi.org/10.1109/X', title='T')
        self.assertEqual(source_agreement_state([c1, c2]), 'agree')

    def test_disagree(self):
        c1 = CandidateInput(source='crossref', doi='10.1109/x', title='T')
        c2 = CandidateInput(source='openalex', doi='10.1016/y', title='T')
        self.assertEqual(source_agreement_state([c1, c2]), 'disagree')

    def test_one_has_doi_one_no_doi(self):
        c1 = CandidateInput(source='crossref', doi='10.1109/x', title='T')
        c2 = CandidateInput(source='openalex', doi=None, title='T')
        self.assertEqual(source_agreement_state([c1, c2]), 'one_has_doi_one_no_doi')

    def test_neither_has_doi_is_single_source(self):
        c1 = CandidateInput(source='crossref', doi=None, title='T')
        c2 = CandidateInput(source='openalex', doi=None, title='T')
        self.assertEqual(source_agreement_state([c1, c2]), 'single_source')


class NoIOGuaranteeTests(unittest.TestCase):
    """Static checks that the resolver module cannot perform DB/network
    I/O -- enforced by absence of the relevant imports, not by mocking."""

    def test_resolver_module_has_no_io_imports(self):
        import resolver
        with open(resolver.__file__, encoding='utf-8') as f:
            src = f.read()
        for forbidden in ('import psycopg2', 'import django', 'from django',
                           'import requests', 'import httpx', 'urlopen'):
            self.assertNotIn(forbidden, src)

    def test_resolve_accepts_no_known_dois_and_still_works(self):
        paper = PaperInput(title='Some Title', year=2020)
        cand = CandidateInput(source='crossref', doi='10.1/x', title='Some Title', year=2020)
        result = resolve(paper, [cand])  # known_dois omitted entirely
        self.assertIn(result.decision, (AUTO_MATCH, REVIEW, REJECT))


# -----------------------------------------------------------------------------
# The 8 mandatory regression cases (Phase 2 audit benchmark)
# -----------------------------------------------------------------------------

def _load_benchmark_cases():
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import benchmark  # local, sibling module -- no network/DB (see its own docstring)
    return benchmark.CASES


class RegressionBenchmarkTests(unittest.TestCase):
    """One test per known case, so a future change that breaks any single
    one fails loudly and by name -- not just as an aggregate count."""

    @classmethod
    def setUpClass(cls):
        cls.cases = {c.paper_id: c for c in _load_benchmark_cases()}

    def _assert_decision(self, paper_id, expected):
        case = self.cases[paper_id]
        result = resolve(case.paper, case.candidates, known_dois={})
        self.assertEqual(
            result.decision, expected,
            msg=f'PaperID {paper_id} ({case.label}): expected {expected}, '
                f'got {result.decision} (score={result.score}, '
                f'reasons={result.reasons})',
        )
        return result

    def test_3013_known_wrong_doi_rejected(self):
        self._assert_decision(3013, REJECT)

    def test_5645_known_wrong_doi_rejected(self):
        self._assert_decision(5645, REJECT)

    def test_7618_source_disagreement_no_doi_on_correct_match_is_review(self):
        result = self._assert_decision(7618, REVIEW)
        # Mandatory permanent regression: the resolver must NEVER select
        # the Crossref DOI (Ananth & Chandrasekaran's) for this paper.
        self.assertNotEqual(result.doi, '10.1109/coconet.2015.7411180')

    def test_7620_source_disagreement_one_noisy_is_review(self):
        self._assert_decision(7620, REVIEW)

    def test_7615_source_agreement_but_wrong_author_rejected(self):
        self._assert_decision(7615, REJECT)

    def test_7636_clean_reject(self):
        self._assert_decision(7636, REJECT)

    def test_7598_clean_reject(self):
        self._assert_decision(7598, REJECT)

    def test_3015_known_correct_auto_matched(self):
        result = self._assert_decision(3015, AUTO_MATCH)
        self.assertEqual(result.doi, '10.47191/ijcsrr/v6-i2-93')

    def test_summary_precision_recall_on_n8_benchmark(self):
        """n=8. Manually selected, hand-verified benchmark -- NOT a random
        sample of the 2031-paper population and NOT sufficient on its own
        to claim general production precision. This test only guards
        against regression on these specific known cases."""
        tp = fp = fn = 0
        for paper_id, case in self.cases.items():
            result = resolve(case.paper, case.candidates, known_dois={})
            predicted_positive = result.decision == AUTO_MATCH
            actual_positive = case.expected == AUTO_MATCH
            if predicted_positive and actual_positive:
                tp += 1
            elif predicted_positive and not actual_positive:
                fp += 1
            elif (not predicted_positive) and actual_positive:
                fn += 1
        self.assertEqual(fp, 0, 'False positive AUTO_MATCH on the known benchmark')
        self.assertEqual(fn, 0, 'False negative AUTO_MATCH on the known benchmark')
        self.assertEqual(tp, 1)


class SourceAvailabilitySafetyTests(unittest.TestCase):
    """Phase 2F. Real-world regression: PaperID 7545 (a live Crossref/
    OpenAlex dry run) produced AUTO_MATCH with score=1.0 when OpenAlex
    failed with HTTP 429, but REVIEW with the identical Crossref evidence
    when OpenAlex responded (with an unrelated, correctly-rejected
    candidate). The only difference between the two runs was network
    reliability, not evidence -- a violation of the stated safety
    invariant: "loss of an independent external evidence source must
    never increase confidence in an AUTO_MATCH decision."

    Fixtures below reproduce PaperID 7545 exactly (title/authors/year from
    the live DB row; Crossref/OpenAlex candidate fields captured from the
    Phase 2E dry run -- no network access in this test file).
    """

    OUR_PAPER = PaperInput(
        title='Performance Enhancement of Supervisory Control and Data '
              'Acquisition (SCADA) IEC 60870-5-104 Intrusion Detection Using '
              'Sequence-Aware and Hybrid Deep Learning Models: A Comparative '
              'Evaluation',
        authors=['MAS Arifin', 'D Stiawan', 'S Susanto', 'MY Idris', 'R Budiarto'],
        year=2026, venue=None, publication_type='Research Article',
    )
    CROSSREF_CAND = CandidateInput(
        source='crossref', doi='10.7759/s44389-026-00076-0',
        title='Performance Enhancement of Supervisory Control and Data '
              'Acquisition (SCADA) IEC 60870-5-104 Intrusion Detection Using '
              'Sequence-Aware and Hybrid Deep Learning Models: A Comparative '
              'Evaluation',
        authors=['MAS Arifin', 'D Stiawan', 'S Susanto', 'MY Idris', 'R Budiarto'],
        year=2026, doc_type='journal-article',
    )
    OPENALEX_UNRELATED_CAND = CandidateInput(
        source='openalex', doi='10.1109/access.2020.2994961',
        title='A Review of Research Work on Network-Based SCADA Intrusion '
              'Detection Systems',
        authors=[], year=2020, doc_type='article',
    )

    # -- 1. Crossref-only correct high-confidence candidate -------------

    def test_1_crossref_only_legacy_call_unaffected(self):
        """sources_attempted omitted (legacy caller / DOI self-validation
        style usage) -- behavior must be byte-identical to pre-Phase-2F."""
        r = resolve(self.OUR_PAPER, [self.CROSSREF_CAND], known_dois={})
        self.assertEqual(r.decision, AUTO_MATCH)
        self.assertEqual(r.source_agreement, 'single_source')

    def test_1b_crossref_only_sources_attempted_equals_present_still_auto_match(self):
        """sources_attempted=1 (accurately: only one source genuinely
        existed to query) -- AUTO_MATCH must remain reachable. This is the
        constraint 'do NOT globally disable single-source matching'."""
        r = resolve(self.OUR_PAPER, [self.CROSSREF_CAND], known_dois={},
                    sources_attempted=1)
        self.assertEqual(r.decision, AUTO_MATCH)

    # -- 2. OpenAlex-only correct high-confidence candidate --------------

    def test_2_openalex_only_correct_candidate_auto_matches(self):
        oa_correct = CandidateInput(
            source='openalex', doi='10.7759/s44389-026-00076-0',
            title=self.OUR_PAPER.title, authors=self.OUR_PAPER.authors,
            year=2026, doc_type='article',
        )
        r = resolve(self.OUR_PAPER, [oa_correct], known_dois={}, sources_attempted=1)
        self.assertEqual(r.decision, AUTO_MATCH)

    # -- 3. Both sources agree -------------------------------------------

    def test_3_both_sources_agree_auto_matches_regardless_of_sources_attempted(self):
        oa_same_doi = CandidateInput(
            source='openalex', doi='10.7759/s44389-026-00076-0',
            title=self.OUR_PAPER.title, authors=self.OUR_PAPER.authors,
            year=2026, doc_type='article',
        )
        r = resolve(self.OUR_PAPER, [self.CROSSREF_CAND, oa_same_doi],
                    known_dois={}, sources_attempted=2)
        self.assertEqual(r.decision, AUTO_MATCH)
        self.assertEqual(r.source_agreement, 'agree')

    # -- 4. Sources disagree (unaffected by this fix -- pre-existing gate)

    def test_4_sources_disagree_stays_review_with_or_without_sources_attempted(self):
        r1 = resolve(self.OUR_PAPER, [self.CROSSREF_CAND, self.OPENALEX_UNRELATED_CAND],
                     known_dois={})
        r2 = resolve(self.OUR_PAPER, [self.CROSSREF_CAND, self.OPENALEX_UNRELATED_CAND],
                     known_dois={}, sources_attempted=2)
        self.assertEqual(r1.decision, REVIEW)
        self.assertEqual(r2.decision, REVIEW)
        self.assertEqual(r1.to_dict(), r2.to_dict())  # sources_attempted must not change an already-2-candidate case

    # -- 5/6. One source unavailable / HTTP 429 represented as missing ---
    #         evidence -- THE CORE PHASE 2F REGRESSION

    def test_5_reproduces_paperid_7545_before_fix_would_have_auto_matched(self):
        """Documents the exact bug: without sources_attempted, this call
        (Crossref succeeded, OpenAlex silently absent because it 429'd)
        AUTO_MATCHes -- this assertion describes the PRE-existing legacy
        contract, not an endorsement. See test_6 for the fix."""
        r = resolve(self.OUR_PAPER, [self.CROSSREF_CAND], known_dois={})
        self.assertEqual(r.decision, AUTO_MATCH)

    def test_6_paperid_7545_with_sources_attempted_no_longer_auto_matches(self):
        """THE FIX. Same evidence as test_5, but the caller correctly
        reports that 2 sources were attempted and only 1 responded. The
        safety invariant must now hold: this must NOT be AUTO_MATCH."""
        r = resolve(self.OUR_PAPER, [self.CROSSREF_CAND], known_dois={},
                    sources_attempted=2)
        self.assertNotEqual(r.decision, AUTO_MATCH)
        self.assertEqual(r.decision, REVIEW)
        self.assertEqual(r.source_agreement, 'single_source_after_failure')
        self.assertIn('source_unavailable_reduced_confidence', r.reasons)

    def test_6b_lost_source_score_still_below_disagreement_floor_rejects(self):
        """A weak single surviving candidate, with a source known to be
        lost, must REJECT (not REVIEW) -- same floor as genuine
        disagreement, no new threshold invented."""
        weak_paper = PaperInput(title='Some Weak Match Title', year=2020)
        weak_cand = CandidateInput(source='crossref', doi='10.1109/weak.example',
                                    title='A Completely Different Paper', year=2020)
        r = resolve(weak_paper, [weak_cand], known_dois={}, sources_attempted=2)
        self.assertEqual(r.decision, REJECT)

    # -- 7. Missing DOI (unaffected -- different branch, pre-existing) ---

    def test_7_missing_doi_unaffected_by_sources_attempted(self):
        no_doi_cand = CandidateInput(source='openalex', doi=None,
                                      title=self.OUR_PAPER.title,
                                      authors=self.OUR_PAPER.authors, year=2026)
        r = resolve(self.OUR_PAPER, [no_doi_cand], known_dois={}, sources_attempted=2)
        self.assertNotEqual(r.decision, AUTO_MATCH)

    # -- 9. Existing thesis/dissertation gate (unaffected) ---------------

    def test_9_thesis_gate_unaffected_by_sources_attempted(self):
        thesis_paper = PaperInput(title='Distributed Systems: A PhD Thesis')
        cand = CandidateInput(source='crossref', doi='10.1109/x',
                               title='Distributed Systems')
        r = resolve(thesis_paper, [cand], known_dois={}, sources_attempted=1)
        self.assertEqual(r.decision, REJECT)
        self.assertTrue(any('type_conflict' in reason for reason in r.reasons))

    # -- 10. Existing duplicate DOI protection (unaffected) --------------

    def test_10_duplicate_doi_protection_unaffected_by_sources_attempted(self):
        r = resolve(self.OUR_PAPER, [self.CROSSREF_CAND],
                    known_dois={'10.7759/s44389-026-00076-0': 9999},
                    sources_attempted=1)
        self.assertEqual(r.decision, REJECT)
        self.assertTrue(any('duplicate_doi' in reason for reason in r.reasons))


if __name__ == '__main__':
    unittest.main()
