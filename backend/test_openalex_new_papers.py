"""Regression tests for openalex_new_papers.py's pagination-completeness
safety fix (openalex_works_with_status() + process_researcher()'s Gate 1).

Run: cd backend && python -m unittest test_openalex_new_papers -v

No DB, no network. openalex_get is mocked throughout. These exist to prove
the exact silent-truncation gap found during code review -- a fetch that
stops because max_pages was exhausted while OpenAlex still had more data
(next_cursor non-empty), or because an openalex_get() call failed outright,
must never be indistinguishable from a genuine, complete result.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scrapers'))

from openalex_new_papers import (  # noqa: E402
    FETCH_FAILED, PAGINATION_COMPLETE, PAGINATION_INCOMPLETE,
    openalex_works_with_status, process_researcher,
)


def _page(results, next_cursor=None):
    return {'results': results, 'meta': {'next_cursor': next_cursor}}


def _work(i):
    return {'id': f'https://openalex.org/W{i}', 'title': f'Work {i}', 'doi': None}


class PaginationStatusTests(unittest.TestCase):
    """openalex_works_with_status() -- pure retrieval-status logic."""

    @patch('openalex_new_papers.openalex_get')
    def test_1_results_end_before_the_cap_is_complete(self, mock_get):
        # 2 pages, second page has no next_cursor -- genuine end, well
        # inside max_pages=10.
        mock_get.side_effect = [
            _page([_work(1), _work(2)], next_cursor='c2'),
            _page([_work(3)], next_cursor=None),
        ]
        works, status = openalex_works_with_status('A1', max_pages=10)
        self.assertEqual(status, PAGINATION_COMPLETE)
        self.assertEqual(len(works), 3)
        self.assertEqual(mock_get.call_count, 2)

    @patch('openalex_new_papers.openalex_get')
    def test_2_last_page_ends_naturally_exactly_at_the_cap_boundary(self, mock_get):
        # max_pages=3: exactly 3 pages fetched, the 3rd one ending with no
        # next_cursor -- the natural-end check must fire on the SAME
        # iteration that also happens to be the last allowed one, not be
        # shadowed by the loop simply running out.
        mock_get.side_effect = [
            _page([_work(1)], next_cursor='c2'),
            _page([_work(2)], next_cursor='c3'),
            _page([_work(3)], next_cursor=None),
        ]
        works, status = openalex_works_with_status('A1', max_pages=3)
        self.assertEqual(status, PAGINATION_COMPLETE)
        self.assertEqual(len(works), 3)
        self.assertEqual(mock_get.call_count, 3)

    @patch('openalex_new_papers.openalex_get')
    def test_3_cap_reached_with_cursor_still_present_is_incomplete(self, mock_get):
        # max_pages=2: both pages come back FULL with a live next_cursor --
        # OpenAlex is explicitly saying there's more, we just stopped asking.
        mock_get.side_effect = [
            _page([_work(1)], next_cursor='c2'),
            _page([_work(2)], next_cursor='c3'),  # still more after this
        ]
        works, status = openalex_works_with_status('A1', max_pages=2)
        self.assertEqual(status, PAGINATION_INCOMPLETE)
        self.assertEqual(len(works), 2)  # partial data IS returned...
        self.assertEqual(mock_get.call_count, 2)  # ...but caller must not trust it as complete

    @patch('openalex_new_papers.openalex_get')
    def test_4_fetch_failure_on_first_page(self, mock_get):
        mock_get.return_value = None  # openalex_get()'s own contract on giving up
        works, status = openalex_works_with_status('A1', max_pages=10)
        self.assertEqual(status, FETCH_FAILED)
        self.assertEqual(works, [])

    @patch('openalex_new_papers.openalex_get')
    def test_4b_fetch_failure_after_partial_success(self, mock_get):
        # Page 1 succeeds (2 works accumulate), page 2's call fails outright.
        # The PaperID-7545-shaped danger: 2 real works were fetched, so a
        # naive caller could easily mistake this partial result for the
        # whole truth.
        mock_get.side_effect = [
            _page([_work(1), _work(2)], next_cursor='c2'),
            None,
        ]
        works, status = openalex_works_with_status('A1', max_pages=10)
        self.assertEqual(status, FETCH_FAILED)
        self.assertEqual(len(works), 2)  # partial data preserved for diagnostics...
        # ...but status, not len(works), is what a caller must gate on.

    @patch('openalex_new_papers.openalex_get')
    def test_5_author_with_more_than_max_pages_worth_of_works_is_incomplete_not_silent(self, mock_get):
        # The concrete >2000-works scenario: every one of the default 10
        # pages comes back full with a live cursor. Must never be reported
        # as PAGINATION_COMPLETE just because the loop ran out of budget.
        mock_get.side_effect = [
            _page([_work(i)], next_cursor=f'c{i+1}') for i in range(10)
        ]
        works, status = openalex_works_with_status('A1', max_pages=10)
        self.assertEqual(status, PAGINATION_INCOMPLETE)
        self.assertEqual(len(works), 10)
        self.assertEqual(mock_get.call_count, 10)  # never fetches an 11th page


class ProcessResearcherGateTests(unittest.TestCase):
    """process_researcher()'s integration of the new Gate 1 -- proves an
    incomplete/failed fetch never reaches 'applied'/'dry_run' and never
    updates LastSyncedAt, exactly like the existing suspected_fetch_failure
    contract for Gate 2 (the expected_count comparison)."""

    class _FakeCursor:
        """Minimal cursor that records every SQL statement executed, so a
        test can assert LastSyncedAt was (or wasn't) ever touched, without
        a real DB connection."""
        def __init__(self):
            self.executed = []

        def execute(self, sql, params=None):
            self.executed.append(sql)

        def fetchone(self):
            return None

    def _lastsynced_was_updated(self, cur):
        return any('LastSyncedAt' in sql for sql in cur.executed)

    @patch('openalex_new_papers.openalex_get')
    def test_6_pagination_incomplete_yields_suspected_fetch_failure_no_lastsynced_update(self, mock_get):
        # authors/{id} lookup succeeds; works fetch hits the cap with more
        # data waiting.
        mock_get.side_effect = [
            {'works_count': 5},  # authors/{id}
            _page([_work(1)], next_cursor='c2'),  # works page 1/1 (max_pages=1 not used here;
            _page([_work(2)], next_cursor='c3'),  # default max_pages=10 in process_researcher
        ] + [_page([_work(i)], next_cursor=f'c{i+1}') for i in range(3, 11)]
        cur = self._FakeCursor()
        record = process_researcher(cur, user_id=1, name='X', openalex_id='A1', apply_mode=True)
        self.assertEqual(record['status'], 'suspected_fetch_failure')
        self.assertEqual(record['pagination_status'], PAGINATION_INCOMPLETE)
        self.assertFalse(self._lastsynced_was_updated(cur))

    @patch('openalex_new_papers.openalex_get')
    def test_7_fetch_failed_yields_suspected_fetch_failure_no_lastsynced_update(self, mock_get):
        # Both authors/{id} AND the works fetch fail outright -- the exact
        # double-failure scenario identified during review. expected_count
        # ends up None, so Gate 2 alone could never have caught this;
        # Gate 1 must.
        mock_get.side_effect = [None, None]
        cur = self._FakeCursor()
        record = process_researcher(cur, user_id=1, name='X', openalex_id='A1', apply_mode=True)
        self.assertEqual(record['status'], 'suspected_fetch_failure')
        self.assertEqual(record['pagination_status'], FETCH_FAILED)
        self.assertFalse(self._lastsynced_was_updated(cur))

    @patch('openalex_new_papers.openalex_get')
    def test_8_pagination_complete_with_no_count_gap_proceeds_normally(self, mock_get):
        mock_get.side_effect = [
            {'works_count': 2},  # authors/{id}
            _page([_work(1), _work(2)], next_cursor=None),  # works, completes in one page
        ]
        cur = self._FakeCursor()
        record = process_researcher(cur, user_id=1, name='X', openalex_id='A1', apply_mode=False)
        self.assertEqual(record['pagination_status'], PAGINATION_COMPLETE)
        self.assertIn(record['status'], ('dry_run', 'applied'))
        self.assertNotEqual(record['status'], 'suspected_fetch_failure')

    @patch('openalex_new_papers.openalex_get')
    def test_10_neither_early_exit_path_issues_a_single_cursor_call(self, mock_get):
        """Stronger than checking for 'LastSyncedAt' alone: proves ZERO
        cur.execute() calls of any kind (no SAVEPOINT, no Authors/
        ResearchPaper write via upsert_work/link_author, no LastSyncedAt
        update) happen on either early-exit path. process_researcher()
        never references `cur` before Gate 1/Gate 2's `return record` --
        this is a structural guarantee from code order, not a runtime
        check, but this test pins it as a regression trip-wire."""
        # PAGINATION_INCOMPLETE path (cap hit with cursor still live).
        mock_get.side_effect = (
            [{'works_count': 5}]
            + [_page([_work(i)], next_cursor=f'c{i+1}') for i in range(10)]
        )
        cur = self._FakeCursor()
        record = process_researcher(cur, user_id=1, name='X', openalex_id='A1', apply_mode=True)
        self.assertEqual(record['status'], 'suspected_fetch_failure')
        self.assertEqual(record['pagination_status'], PAGINATION_INCOMPLETE)
        self.assertEqual(cur.executed, [])  # zero SQL statements of ANY kind

        # FETCH_FAILED path (both authors/{id} and works fetch fail).
        mock_get.side_effect = [None, None]
        cur2 = self._FakeCursor()
        record2 = process_researcher(cur2, user_id=1, name='X', openalex_id='A1', apply_mode=True)
        self.assertEqual(record2['status'], 'suspected_fetch_failure')
        self.assertEqual(record2['pagination_status'], FETCH_FAILED)
        self.assertEqual(cur2.executed, [])  # zero SQL statements of ANY kind

    @patch('openalex_new_papers.openalex_get')
    def test_9_expected_count_gap_guard_still_fires_independently_of_pagination_status(self, mock_get):
        # Pagination completes cleanly (Gate 1 passes) but the count-gap
        # guard (Gate 2, pre-existing) must still catch a suspicious gap on
        # its own -- the two gates are complementary, neither replaces the
        # other.
        mock_get.side_effect = [
            {'works_count': 100},  # authors/{id} says 100...
            _page([_work(1), _work(2)], next_cursor=None),  # ...but only 2 come back, cleanly
        ]
        cur = self._FakeCursor()
        record = process_researcher(cur, user_id=1, name='X', openalex_id='A1', apply_mode=True)
        self.assertEqual(record['pagination_status'], PAGINATION_COMPLETE)  # Gate 1: fine
        self.assertEqual(record['status'], 'suspected_fetch_failure')  # Gate 2: still catches it
        self.assertFalse(self._lastsynced_was_updated(cur))


if __name__ == '__main__':
    unittest.main()
