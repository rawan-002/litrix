"""
OpenAlex scraper for researchers who have NO Google Scholar profile.

=============================================================================
WHY THIS EXISTS (see LITRIX_AI_CHATBOT.md-adjacent planning notes, 2026-08-09)
=============================================================================
The existing "Scrape" button (scholar_new_papers.py, wired via
accounts/sync_views.py::trigger_new_papers_scrape) only covers researchers
with a Scholar_ID. Anyone with a confirmed Researcher.OpenAlex_AuthorID but
NO Scholar_ID was invisible to it entirely. This script closes that gap as
a THIRD step in the same job chain - scholar_new_papers.py is UNTOUCHED.

STRICT ISOLATION (non-negotiable, confirmed with the user):
    WHERE r."OpenAlex_AuthorID" IS NOT NULL
      AND (u."Scholar_ID" IS NULL OR u."Scholar_ID" = '')
A researcher with BOTH identifiers is Scholar's alone - never touched here,
never scraped twice. No identifier is ever guessed, searched for, or
created in this script - only identifiers already confirmed in the DB
(by the separate discovery/staging/merge pipeline) are used.

Unlike scholar_new_papers.py, this does NOT do a "stop at the first known
paper" cutoff - that optimization exists purely to save PAID SerpAPI
credits, and OpenAlex is free. Instead it fetches the researcher's full
work list every time and relies on upsert_work's own DOI/OpenAlexWorkID/
title dedup (see scrapers/orcid.py) to insert only genuinely new papers -
same end result ("only new papers land in the DB"), simpler mechanism.

FAILURE VISIBILITY (the actual bug this exists to not repeat): a raw
author-lookup call first records the author's real `works_count`. If the
full fetch afterward returns fewer works than a large gap would explain,
that researcher is flagged 'suspected_fetch_failure' rather than silently
reported as "found N papers" - and LastSyncedAt is deliberately NOT
updated for them, so the next run retries automatically instead of the
gap being mistaken for "this person just doesn't have more papers."

One researcher's exception never aborts the batch - it's caught, logged
into that researcher's own report entry, and the loop continues.

USAGE:
    python scrapers/openalex_new_papers.py --dry-run            # report only
    python scrapers/openalex_new_papers.py --apply              # all eligible
    python scrapers/openalex_new_papers.py --apply --user 106   # one researcher
"""
import sys
import time
import json
import argparse
from pathlib import Path
from datetime import date, datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))
from orcid import (  # noqa: E402
    db, openalex_get, normalize_title,
    upsert_work, link_author, determine_author_order,
)

INTER_RESEARCHER_SLEEP = 0.6  # be gentle with OpenAlex's free/polite pool

# -----------------------------------------------------------------------------
# Pagination-status-aware works fetch
# -----------------------------------------------------------------------------
# Deliberately NOT a change to orcid.py's openalex_works_by_author_id() --
# that function has other live, already-committed call sites (orcid.py's own
# --openalex-id CLI path, an internal ORCID/OpenAlex merge helper, and
# scrapers/scopus.py) that this fix must not touch or risk. This is a
# separate, narrowly-scoped function used only by this script.
#
# The bug this exists to close: orcid.openalex_works_by_author_id() returns
# a bare list in ALL THREE of these cases, identically and silently:
#   (a) genuine completion (cursor/results ran out naturally)
#   (b) an openalex_get() call failed outright
#   (c) max_pages was exhausted while OpenAlex's own next_cursor said there
#       was still more data waiting (i.e. this specific author has more than
#       max_pages * per-page works)
# A caller has no way to tell (a) apart from (b)/(c) from the return value
# alone -- a truncated/failed fetch looks exactly like "this researcher
# genuinely has this many papers, no more."
PAGINATION_COMPLETE = 'pagination_complete'
PAGINATION_INCOMPLETE = 'pagination_incomplete'
FETCH_FAILED = 'fetch_failed'


def openalex_works_with_status(author_id, max_pages=10):
    """Same retrieval logic and endpoint as orcid.openalex_works_by_author_id()
    (per-page 200, cursor pagination), but returns (works, status) instead of
    a bare list. `status` is one of PAGINATION_COMPLETE / PAGINATION_INCOMPLETE
    / FETCH_FAILED -- see module-level note above for what each means. Only
    PAGINATION_COMPLETE means `works` is safe to treat as this author's full,
    current work list; the other two mean `works` (even if non-empty, from
    pages fetched before a later failure) must not be trusted as complete.
    """
    works = []
    cursor = "*"
    aid = author_id.strip()
    if not aid.startswith("A") and not aid.startswith("https://"):
        aid = "A" + aid.lstrip("A")
    for _ in range(max_pages):
        data = openalex_get("works", {
            "filter": f"author.id:{aid}",
            "per-page": 200,
            "cursor": cursor,
        })
        if not data:
            return works, FETCH_FAILED
        results = data.get("results") or []
        works.extend(results)
        cursor = (data.get("meta") or {}).get("next_cursor")
        if not cursor or not results:
            return works, PAGINATION_COMPLETE
        time.sleep(0.2)
    # max_pages exhausted while a cursor (and results) were still present on
    # the last page fetched -- there IS more data we chose not to fetch.
    return works, PAGINATION_INCOMPLETE


def _eligible_researchers(cur, only_user=None):
    where = (
        'r."OpenAlex_AuthorID" IS NOT NULL AND r."OpenAlex_AuthorID" != \'\' '
        'AND (u."Scholar_ID" IS NULL OR u."Scholar_ID" = \'\')'
    )
    params = []
    if only_user:
        where += ' AND u."UserID" = %s'
        params.append(only_user)
    cur.execute(f'''
        SELECT u."UserID", u."FullName_Ar", r."OpenAlex_AuthorID"
        FROM "Users" u JOIN "Researcher" r ON r."UserID" = u."UserID"
        WHERE u."UserType" = 'Researcher' AND {where}
        ORDER BY u."UserID"
    ''', params)
    return cur.fetchall()


def _would_be_new(cur, w):
    """Read-only mirror of upsert_work's own dedup priority (DOI ->
    OpenAlexWorkID -> title) so --dry-run can report an honest new-vs-
    existing preview without writing anything - matches the real merge
    logic exactly, not a looser approximation."""
    doi = (w.get('doi') or '').replace('https://doi.org/', '').lower() or None
    openalex_work_id = (w.get('id') or '').replace('https://openalex.org/', '') or None
    title = (w.get('title') or '').strip()
    if not title:
        return False  # upsert_work would skip it too - not "new", just unusable
    if doi:
        cur.execute('SELECT 1 FROM "ResearchPaper" WHERE LOWER("DOI") = %s LIMIT 1', (doi,))
        if cur.fetchone():
            return False
    if openalex_work_id:
        cur.execute('SELECT 1 FROM "ResearchPaper" WHERE "OpenAlexWorkID" = %s LIMIT 1', (openalex_work_id,))
        if cur.fetchone():
            return False
    norm = normalize_title(title)
    cur.execute(
        'SELECT 1 FROM "ResearchPaper" WHERE "NormalizedTitle" = %s OR LOWER("Title") = LOWER(%s) LIMIT 1',
        (norm, title),
    )
    return cur.fetchone() is None


def process_researcher(cur, user_id, name, openalex_id, apply_mode):
    """Returns a run-report dict for this one researcher. Never raises -
    any failure is captured into the dict's error/status fields instead."""
    started_at = datetime.now(timezone.utc).isoformat()
    record = {
        'researcher_id': user_id, 'name': name, 'identifier_used': openalex_id,
        'source': 'openalex', 'started_at': started_at, 'finished_at': None,
        'papers_found': 0, 'papers_new': 0, 'papers_updated': 0,
        'papers_failed': 0, 'status': None, 'error': None,
        'pagination_status': None,
    }
    try:
        author = openalex_get(f'authors/{openalex_id}')
        expected_count = (author or {}).get('works_count')

        works, pagination_status = openalex_works_with_status(openalex_id)
        record['papers_found'] = len(works)
        record['pagination_status'] = pagination_status

        # Gate 1 (new): the fetch itself must have completed cleanly. A
        # truncated or failed fetch returns the exact same shape as a
        # genuine result -- pagination_status is the only thing that tells
        # them apart. This catches both an outright API failure AND an
        # author with more works than max_pages*per-page can reach (neither
        # of which the works_count comparison below is guaranteed to catch:
        # see openalex_works_with_status()'s docstring).
        if pagination_status != PAGINATION_COMPLETE:
            record['status'] = 'suspected_fetch_failure'
            record['error'] = (
                f'works fetch did not complete cleanly (pagination_status='
                f'{pagination_status}, papers_found_so_far={len(works)}) - '
                f'treating as an unreliable fetch, not a real result. '
                f'LastSyncedAt will NOT be updated so the next run retries.'
            )
            record['finished_at'] = datetime.now(timezone.utc).isoformat()
            return record

        # Gate 2 (pre-existing): the exact failure mode from the 2026-08-09
        # session: a fetch that COMPLETES (per Gate 1) but silently returns
        # far fewer works than the author actually has, e.g. because the
        # author-lookup and works-list calls saw an inconsistent snapshot.
        # A wide gap (not just "one less than expected") is the signal -
        # OpenAlex's works_count itself can be a hair stale, so don't flag
        # on trivial differences. Independent of Gate 1 - either can fire
        # on its own.
        if expected_count and len(works) < expected_count * 0.5:
            record['status'] = 'suspected_fetch_failure'
            record['error'] = (
                f'author reports works_count={expected_count} but fetch '
                f'returned only {len(works)} - treating as an unreliable '
                f'fetch, not a real result. LastSyncedAt will NOT be '
                f'updated so the next run retries.'
            )
            record['finished_at'] = datetime.now(timezone.utc).isoformat()
            return record

        for w in works:
            if not apply_mode:
                if _would_be_new(cur, w):
                    record['papers_new'] += 1
                else:
                    record['papers_updated'] += 1
                continue
            try:
                cur.execute('SAVEPOINT sp_researcher_paper')
                paper_id, was_new = upsert_work(cur, w, user_id)
                if not paper_id:
                    cur.execute('ROLLBACK TO SAVEPOINT sp_researcher_paper')
                    continue
                order, name_raw = determine_author_order(w, openalex_author_id=openalex_id)
                link_author(cur, user_id, paper_id, 'openalex_author_id', name_raw)
                cur.execute('RELEASE SAVEPOINT sp_researcher_paper')
                record['papers_new' if was_new else 'papers_updated'] += 1
            except Exception as e:
                cur.execute('ROLLBACK TO SAVEPOINT sp_researcher_paper')
                record['papers_failed'] += 1
                print(f'    [error] one paper for {name}: {e}')

        if apply_mode:
            cur.execute('UPDATE "Researcher" SET "LastSyncedAt" = NOW() WHERE "UserID" = %s', [user_id])
        record['status'] = 'applied' if apply_mode else 'dry_run'
    except Exception as e:
        record['status'] = 'error'
        record['error'] = f'{type(e).__name__}: {e}'

    record['finished_at'] = datetime.now(timezone.utc).isoformat()
    return record


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true',
                      help='Fetch + report what WOULD happen. Writes nothing.')
    mode.add_argument('--apply', action='store_true',
                      help='Insert new papers + author links, update LastSyncedAt.')
    ap.add_argument('--user', type=int, default=None, help='One researcher only (UserID).')
    args = ap.parse_args()

    conn = db()
    cur = conn.cursor()
    researchers = _eligible_researchers(cur, only_user=args.user)
    print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'} | "
          f"OpenAlex-only eligible researchers: {len(researchers)}\n")

    run_log = []
    for i, (user_id, name, openalex_id) in enumerate(researchers):
        if i:
            time.sleep(INTER_RESEARCHER_SLEEP)
        record = process_researcher(cur, user_id, name, openalex_id, args.apply)
        if args.apply:
            conn.commit()  # per-researcher commit -> safe to interrupt/resume
        run_log.append(record)

        label = record['status']
        print(f"[{label}] {name} (UID {user_id}, {openalex_id}): "
              f"found={record['papers_found']} new={record['papers_new']} "
              f"updated={record['papers_updated']} failed={record['papers_failed']}"
              + (f" -- {record['error']}" if record['error'] else ''))

    today = date.today().isoformat()
    out_path = Path(__file__).resolve().parent.parent / 'reports' / (
        f"openalex_new_papers_{today}{'_apply' if args.apply else '_dryrun'}.json"
    )
    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump({'mode': 'apply' if args.apply else 'dry_run', 'runs': run_log}, f,
                   ensure_ascii=False, indent=2)

    n_ok = sum(1 for r in run_log if r['status'] in ('dry_run', 'applied'))
    n_suspect = sum(1 for r in run_log if r['status'] == 'suspected_fetch_failure')
    n_err = sum(1 for r in run_log if r['status'] == 'error')
    total_new = sum(r['papers_new'] for r in run_log)
    total_updated = sum(r['papers_updated'] for r in run_log)
    total_failed_papers = sum(r['papers_failed'] for r in run_log)
    print(f"\nWrote {out_path}")
    print(f"{n_ok} ok, {n_suspect} suspected fetch failures (not retried automatically "
          f"until next run), {n_err} errored")
    print(f"Papers: {total_new} new, {total_updated} enriched, {total_failed_papers} failed")
    if not args.apply:
        print('This was a DRY RUN - no database writes happened. Pass --apply to write.')

    cur.close()
    conn.close()


if __name__ == '__main__':
    main()
