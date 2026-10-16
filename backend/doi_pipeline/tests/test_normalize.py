"""Unit tests for doi_pipeline/normalize.py -- pure functions, no I/O.

Run directly:
    python backend/doi_pipeline/tests/test_normalize.py
Or via pytest (if installed):
    python -m pytest backend/doi_pipeline/tests -q
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # doi_pipeline/

from normalize import (  # noqa: E402
    author_surname,
    is_same_doi,
    normalize_author,
    normalize_doi,
    normalize_pages,
    normalize_title,
    normalize_venue,
    title_token_jaccard,
    title_tokens,
)


class NormalizeTitle(unittest.TestCase):
    def test_none_and_empty_are_safe(self):
        self.assertEqual(normalize_title(None), '')
        self.assertEqual(normalize_title(''), '')

    def test_lowercase_and_whitespace_collapse(self):
        self.assertEqual(normalize_title('  A   Title   Here  '), 'a title here')

    def test_diacritics_stripped(self):
        self.assertEqual(normalize_title('Café Résumé'), 'cafe resume')

    def test_curly_apostrophe_normalized(self):
        self.assertEqual(
            normalize_title('Corrigendum to “Optimized RNN”'),
            "corrigendum to optimized rnn",
        )

    def test_dash_variants_normalized_to_hyphen(self):
        # en dash, em dash, minus sign all -> '-'
        for dash in ('–', '—', '−'):
            self.assertIn('theory-based', normalize_title(f'Theory{dash}based Approach'))

    def test_no_stemming_of_related_scientific_terms(self):
        # "networked" and "network" must NOT collapse to the same token --
        # PaperID 7630's real ambiguity (Observation-Interface vs Sensor)
        # depends on this kind of distinction surviving normalization.
        self.assertNotEqual(normalize_title('networked'), normalize_title('network'))


class TitleTokensAndJaccard(unittest.TestCase):
    def test_stopwords_removed(self):
        toks = title_tokens('A Study of the Approach for Cloud Computing')
        self.assertNotIn('a', toks)
        self.assertNotIn('the', toks)
        self.assertNotIn('of', toks)
        self.assertNotIn('for', toks)
        self.assertIn('study', toks)
        self.assertIn('approach', toks)
        self.assertIn('cloud', toks)
        self.assertIn('computing', toks)

    def test_jaccard_identical_titles_is_one(self):
        t = 'Cooperative game theory-based approach for energy-aware job scheduling in cloud'
        self.assertEqual(title_token_jaccard(t, t), 1.0)

    def test_jaccard_empty_either_side_is_zero(self):
        self.assertEqual(title_token_jaccard('', 'Something'), 0.0)
        self.assertEqual(title_token_jaccard('Something', None), 0.0)
        self.assertEqual(title_token_jaccard(None, None), 0.0)

    def test_known_false_candidate_pair_scores_low(self):
        """The exact pair from the Phase 0 audit (PaperID 7618's wrong
        Crossref candidate): SequenceMatcher scored this 0.779 (above the
        old, unsafe 0.72 threshold). Token-Jaccard must score it well
        below the 0.90 AUTO_MATCH_THRESHOLD -- this is why Jaccard was
        chosen as the primary metric."""
        ours = ('A Cooperative Game Theory-based Approach for Energy-Aware '
                'Job Scheduling in Cloud.')
        wrong_candidate = ('Cooperative game theoretic approach for job '
                            'scheduling in cloud computing')
        score = title_token_jaccard(ours, wrong_candidate)
        self.assertLess(score, 0.60)

    def test_known_correct_pair_scores_high(self):
        t1 = ('The Feasibility of Using Random Administrative Samples (The '
              'French Experiment) in Population Censuses in African Countries')
        t2 = t1  # Crossref-confirmed exact title match (PaperID 3015)
        self.assertGreaterEqual(title_token_jaccard(t1, t2), 0.90)


class NormalizeAuthor(unittest.TestCase):
    def test_none_safe(self):
        self.assertEqual(normalize_author(None), '')

    def test_diacritics_and_case(self):
        self.assertEqual(normalize_author('José García'), 'jose garcia')

    def test_surname_extraction(self):
        self.assertEqual(author_surname('Saad Alqithami'), 'alqithami')
        self.assertEqual(author_surname('K. Chandrasekaran'), 'chandrasekaran')

    def test_surname_empty_when_no_usable_token(self):
        self.assertEqual(author_surname(None), '')
        self.assertEqual(author_surname(''), '')
        self.assertEqual(author_surname('A.'), '')  # single-letter token only


class NormalizeDoi(unittest.TestCase):
    def test_none_and_empty(self):
        self.assertIsNone(normalize_doi(None))
        self.assertIsNone(normalize_doi(''))

    def test_bare_doi_unchanged_in_shape(self):
        self.assertEqual(normalize_doi('10.1109/ABC.2020.123'), '10.1109/abc.2020.123')

    def test_https_wrapper_stripped(self):
        self.assertEqual(normalize_doi('https://doi.org/10.1109/ABC.2020.123'),
                          '10.1109/abc.2020.123')

    def test_http_wrapper_stripped(self):
        self.assertEqual(normalize_doi('http://doi.org/10.1109/abc'), '10.1109/abc')

    def test_dx_doi_org_wrapper_stripped(self):
        self.assertEqual(normalize_doi('https://dx.doi.org/10.1109/abc'), '10.1109/abc')

    def test_doi_colon_prefix_stripped(self):
        self.assertEqual(normalize_doi('doi:10.1109/abc'), '10.1109/abc')
        self.assertEqual(normalize_doi('DOI: 10.1109/abc'), '10.1109/abc')

    def test_surrounding_whitespace_and_quotes_stripped(self):
        self.assertEqual(normalize_doi('  "10.1109/abc"  '), '10.1109/abc')

    def test_trailing_punctuation_stripped(self):
        for suffix in ('.', ',', ';', ':', ')', ']', "'"):
            self.assertEqual(normalize_doi(f'10.1109/abc{suffix}'), '10.1109/abc',
                              msg=f'suffix={suffix!r}')

    def test_malformed_doi_returns_none_not_a_guess(self):
        self.assertIsNone(normalize_doi('not-a-doi'))
        self.assertIsNone(normalize_doi('-'))
        self.assertIsNone(normalize_doi('10.abc/xyz'))  # prefix must be all-digit

    def test_case_insensitive_by_design(self):
        self.assertEqual(normalize_doi('10.1109/ABC'), normalize_doi('10.1109/abc'))


class IsSameDoi(unittest.TestCase):
    def test_wrapper_and_case_variants_are_the_same_doi(self):
        self.assertTrue(is_same_doi(
            'https://doi.org/10.1109/ABC.2020.123',
            'doi:10.1109/abc.2020.123',
        ))

    def test_different_dois_are_different(self):
        self.assertFalse(is_same_doi('10.1109/abc', '10.1109/xyz'))

    def test_none_never_equals_none(self):
        self.assertFalse(is_same_doi(None, None))
        self.assertFalse(is_same_doi('', ''))


class NormalizeVenue(unittest.TestCase):
    def test_none_safe(self):
        self.assertEqual(normalize_venue(None), '')

    def test_basic_normalization(self):
        self.assertEqual(normalize_venue('IEEE Access'), 'ieee access')


class NormalizePages(unittest.TestCase):
    def test_none_and_empty(self):
        self.assertEqual(normalize_pages(None), '')
        self.assertEqual(normalize_pages(''), '')

    def test_dash_variants_all_equal(self):
        variants = ['123-130', '123–130', '123–130.', '123 - 130']
        normalized = {normalize_pages(v) for v in variants}
        self.assertEqual(len(normalized), 1)
        self.assertEqual(next(iter(normalized)), '123-130')


if __name__ == '__main__':
    unittest.main()
