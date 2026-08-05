"""Regression tests for venue_classifiers.classify_from_doi(). Pure DOI-shape
logic, no DB/network - run with:

  python venue_classifiers/test_venue_classifiers.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from venue_classifiers import BOOK_CHAPTER, JOURNAL, UNKNOWN, classify_from_doi


class WileyBookChapters(unittest.TestCase):
    def test_matches_chapter_doi(self):
        # PaperID 5599 and siblings - "Wiley Data and Cybersecurity, 2021".
        self.assertEqual(classify_from_doi('10.1002/9781119606383.ch3'), BOOK_CHAPTER)
        self.assertEqual(classify_from_doi('10.1002/9780470172339.ch44'), BOOK_CHAPTER)

    def test_does_not_match_non_chapter_wiley_doi(self):
        self.assertEqual(classify_from_doi('10.1002/widm.1249'), UNKNOWN)


class FrontiersResearchTopicEbooks(unittest.TestCase):
    def test_matches_isbn_shaped_ebook_doi(self):
        # PaperID 5526 - "Neuro-detection..." Research Topic compilation,
        # Crossref type='edited-book' but it's a repackaging of already-
        # counted journal articles, not new book content.
        self.assertEqual(classify_from_doi('10.3389/978-2-8325-6894-1'), JOURNAL)

    def test_does_not_match_regular_frontiers_article_doi(self):
        # Real Frontiers articles use a journal-code prefix, never digits.
        self.assertEqual(classify_from_doi('10.3389/fnins.2021.1234567'), UNKNOWN)
        self.assertEqual(classify_from_doi('10.3389/fcomp.2025.1685174'), UNKNOWN)


class SpringerDeliberatelyUnregistered(unittest.TestCase):
    def test_shared_prefix_not_classified_by_pattern_alone(self):
        # 10.1007/978-... is shared with legitimate LNCS/CCIS conference
        # proceedings - must fall through to the Crossref/OpenAlex opinion
        # path in verify_venue_authoritative.py, never a bare DOI-shape rule.
        self.assertEqual(classify_from_doi('10.1007/978-3-030-12345-6_5'), UNKNOWN)


class EdgeCases(unittest.TestCase):
    def test_none_and_empty_doi(self):
        self.assertEqual(classify_from_doi(None), UNKNOWN)
        self.assertEqual(classify_from_doi(''), UNKNOWN)
        self.assertEqual(classify_from_doi('   '), UNKNOWN)


if __name__ == '__main__':
    unittest.main()
