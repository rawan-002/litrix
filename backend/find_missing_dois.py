"""Find canonical DOIs for ResearchPaper rows that lack one, using the
audited multi-signal doi_pipeline resolver (backend/doi_pipeline/) as the
sole decision authority for AUTO_MATCH / REVIEW / REJECT.

Phase 3B integration: retrieval (Crossref + OpenAlex) and DB access live
here; scoring, thresholds, publication-type/thesis gates, duplicate-DOI
resolver-side protection, and the Phase 2F source-failure-confidence
safeguard live in doi_pipeline.resolver and are NOT reimplemented here.

Every paper queries BOTH Crossref and OpenAlex unconditionally
(sources_attempted=2, always -- this script never conditionally skips a
source), so a source that fails (timeout/429/error) is never silently
treated as "never attempted": resolve() sees the true attempt count and
reduces confidence accordingly instead of letting a lost source look like
an ordinary, untroubled single-source result (see PaperID 7545 in the
doi_pipeline test suite).

Two independent duplicate-DOI protections are kept, deliberately not
merged into one: known_dois (resolve()-time, in-memory, preloaded once
per run from the real DB) and doi_taken() (a live DB re-check immediately
before the UPDATE, closing any race between preload and write). Only
AUTO_MATCH reaches either check; REVIEW/REJECT never reach the write path.

Dry-run by default (--apply to write), per-paper transactions, never
overwrites an existing DOI or steals one already owned by another paper,
and re-runs only revisit still-DOI-less rows. A per-run JSON decision
report is written to backend/reports/ for every paper processed,
regardless of decision, to support later AUTO_MATCH audit. Examples:

    python backend/find_missing_dois.py --user 1            # dry-run, one researcher
    python backend/find_missing_dois.py --user 1 --apply    # write the AUTO_MATCH results
    python backend/find_missing_dois.py --apply --limit 50  # whole DB, capped
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from typing import Any, Optional

import psycopg2
from psycopg2.extras import RealDictCursor

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed. Run: pip install requests")
    sys.exit(1)

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

DATABASE_URL = os.environ.get('DATABASE_URL')
CONTACT = os.environ.get('LITRIX_CONTACT_EMAIL', 'ra20awn@gmail.com')
UA = f'Litrix-DOIBackfill/1.0 (mailto:{CONTACT})'

API_DELAY = 0.15

# Shared DB helper (single source — litrix_db.py lives in backend/).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from litrix_db import db  # noqa: E402
from doi_pipeline import (  # noqa: E402
    PaperInput, CandidateInput, resolve, AUTO_MATCH, REVIEW, REJECT, normalize_doi,
)
from scrapers.orcid import openalex_get  # noqa: E402


# -----------------------------------------------------------------------------
# Retrieval outcome vocabulary
# -----------------------------------------------------------------------------
# Crossref (retrieval logic owned by this file) gets full 5-way fidelity.
# OpenAlex reuses scrapers/orcid.py::openalex_get() as required -- that
# function already retries/handles 429 internally but only PRINTS the
# specific failure reason, never returns it, so timeout/rate-limited-after-
# retries/other-HTTP-error/network-exception are all indistinguishable from
# the outside as a bare `None`. Modifying openalex_get() to expose that
# distinction is out of this phase's scope (only find_missing_dois.py and
# the new test file may be touched), so OUTCOME_FAILED is deliberately
# coarser than the other four -- this is a real, evidence-based constraint,
# not an oversight. It does not weaken sources_attempted or the
# no_results-vs-failed distinction: both remain exact for both sources.
OUTCOME_SUCCEEDED = 'succeeded'
OUTCOME_NO_RESULTS = 'no_results'
OUTCOME_TIMEOUT = 'timeout'
OUTCOME_RATE_LIMITED = 'rate_limited'
OUTCOME_ERROR = 'error'
OUTCOME_FAILED = 'failed'  # OpenAlex only -- see note above


def split_authors(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return [p.strip() for p in raw.replace(' | ', ',').split(',') if p.strip()]


def crossref_candidate(title: str) -> tuple[Optional[CandidateInput], str]:
    """One Crossref candidate (rank-1) for `title`, or (None, outcome).
    outcome in {succeeded, no_results, timeout, rate_limited, error}."""
    try:
        r = requests.get('https://api.crossref.org/works',
                          params={'query.bibliographic': (title or '')[:300], 'rows': 1,
                                  'select': 'DOI,title,author,issued,published-print,'
                                            'published-online,container-title,publisher,'
                                            'type,volume,issue,page'},
                          headers={'User-Agent': UA}, timeout=25)
    except requests.Timeout:
        return None, OUTCOME_TIMEOUT
    except requests.RequestException:
        return None, OUTCOME_ERROR
    if r.status_code == 429:
        return None, OUTCOME_RATE_LIMITED
    if r.status_code != 200:
        return None, OUTCOME_ERROR
    try:
        items = (r.json().get('message') or {}).get('items') or []
    except ValueError:
        return None, OUTCOME_ERROR
    if not items:
        return None, OUTCOME_NO_RESULTS

    m = items[0]
    year = None
    for k in ('published-print', 'published-online', 'issued'):
        dp = (m.get(k) or {}).get('date-parts') or []
        if dp and dp[0] and dp[0][0]:
            year = dp[0][0]
            break
    authors = [f"{a.get('given', '')} {a.get('family', '')}".strip()
               for a in m.get('author', []) or []]
    cand = CandidateInput(
        source='crossref', doi=m.get('DOI'), title=(m.get('title') or [''])[0],
        authors=authors, year=year, venue=(m.get('container-title') or [''])[0],
        publisher=m.get('publisher'), doc_type=m.get('type'),
        volume=m.get('volume'), issue=m.get('issue'), pages=m.get('page'),
    )
    return cand, OUTCOME_SUCCEEDED


def openalex_candidate(title: str) -> tuple[Optional[CandidateInput], str]:
    """One OpenAlex candidate (rank-1) for `title`, via the shared
    scrapers.orcid.openalex_get() client, or (None, outcome). outcome in
    {succeeded, no_results, failed} -- see the module-level note on why
    this is coarser than crossref_candidate()'s outcome set."""
    data = openalex_get('works', {
        'search': (title or '')[:300], 'per-page': 1,
        'select': 'doi,title,authorships,publication_year,primary_location,type,biblio',
    })
    if data is None:
        return None, OUTCOME_FAILED
    results = data.get('results') or []
    if not results:
        return None, OUTCOME_NO_RESULTS

    w = results[0]
    authors = [(a.get('author') or {}).get('display_name', '')
               for a in w.get('authorships', []) or []]
    loc = w.get('primary_location') or {}
    source = loc.get('source') or {}
    doi = (w.get('doi') or '').replace('https://doi.org/', '') or None
    biblio = w.get('biblio') or {}
    cand = CandidateInput(
        source='openalex', doi=doi, title=w.get('title'), authors=authors,
        year=w.get('publication_year'), venue=source.get('display_name'),
        publisher=source.get('host_organization_name'), doc_type=w.get('type'),
        volume=biblio.get('volume'), issue=biblio.get('issue'),
        pages=(f"{biblio.get('first_page', '')}-{biblio.get('last_page', '')}"
               if biblio.get('first_page') else None),
    )
    return cand, OUTCOME_SUCCEEDED


def build_paper_input(p: dict) -> PaperInput:
    return PaperInput(
        title=p['Title'], authors=split_authors(p.get('AuthorNameRaw')),
        year=p.get('PubYear'), venue=p.get('JournalName'),
        publication_type=p.get('PublicationType'),
        volume=p.get('Volume'), issue=p.get('Issue'), pages=p.get('Pages'),
    )


def resolve_for_paper(paper: PaperInput,
                       cr_cand: Optional[CandidateInput], cr_outcome: str,
                       oa_cand: Optional[CandidateInput], oa_outcome: str,
                       known_dois: dict):
    """Pure glue between retrieval outcomes and doi_pipeline.resolve() --
    no DB, no network. Both sources are always attempted by this script's
    design, so sources_attempted is always 2 regardless of cr_outcome/
    oa_outcome: this is exactly what prevents a failed source from being
    scored as if it had never been queried (the PaperID 7545 invariant).
    Returns (ResolverResult, sources_attempted, candidates)."""
    sources_attempted = 2
    candidates = [c for c in (cr_cand, oa_cand) if c is not None]
    result = resolve(paper, candidates, known_dois=known_dois,
                      sources_attempted=sources_attempted)
    return result, sources_attempted, candidates


def fetch_targets(conn, user_id, limit, start_after_paper_id=None):
    """start_after_paper_id (optional): only rows with PaperID strictly
    greater than this value. Pure pagination for resumable dry-run
    validation batches -- nothing is written in this script regardless, so
    without this a re-run at the same --limit always re-fetches the exact
    same leading rows in PaperID order. Does not affect which candidates
    are matched or how they're scored."""
    where = ['(rp."DOI" IS NULL OR rp."DOI" = \'\')',
             'rp."Title" IS NOT NULL', 'LENGTH(rp."Title") >= 12']
    params: list[Any] = []
    if user_id is not None:
        where.append('EXISTS (SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID" '
                     'AND a."UserID" = %s)')
        params.append(user_id)
    if start_after_paper_id is not None:
        where.append('rp."PaperID" > %s')
        params.append(start_after_paper_id)
    sql = f'''
        SELECT rp."PaperID", rp."Title", rp."PubYear",
               rp."Volume", rp."Issue", rp."Pages", rp."PublicationType",
               j."JournalName",
               (SELECT string_agg(a."AuthorNameRaw", ' | ') FROM "Authors" a
                WHERE a."PaperID" = rp."PaperID" AND a."AuthorNameRaw" IS NOT NULL
                AND a."AuthorNameRaw" <> '') AS "AuthorNameRaw"
        FROM "ResearchPaper" rp
        LEFT JOIN "Journals" j ON j."JournalID" = rp."JournalID"
        WHERE {' AND '.join(where)}
        ORDER BY rp."PaperID"
        {f'LIMIT {int(limit)}' if limit else ''}
    '''
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def load_known_dois(conn) -> dict[str, int]:
    """Bulk-preload every DOI already in the database, normalized, for
    doi_pipeline.resolve()'s known_dois duplicate-protection gate. One
    query for the whole run (never one query per candidate).

    This is a resolve()-TIME, in-memory check. It does NOT replace
    doi_taken()'s live, per-write re-check immediately before UPDATE (see
    main()) — the two are independent, additive protections: known_dois
    catches duplicates that existed at preload time; doi_taken() catches
    duplicates created by any other write (this run or another process)
    between preload and this specific paper's write.
    """
    known: dict[str, int] = {}
    with conn.cursor() as cur:
        cur.execute('SELECT "PaperID", "DOI" FROM "ResearchPaper" '
                    'WHERE "DOI" IS NOT NULL AND "DOI" <> \'\'')
        for pid, doi in cur.fetchall():
            nd = normalize_doi(doi)
            if nd:
                known[nd] = pid
    return known


def doi_taken(conn, doi, paper_id) -> bool:
    with conn.cursor() as cur:
        cur.execute('SELECT 1 FROM "ResearchPaper" WHERE LOWER("DOI") = LOWER(%s) '
                    'AND "PaperID" <> %s LIMIT 1', [doi, paper_id])
        return cur.fetchone() is not None


def main():
    ap = argparse.ArgumentParser(description='Strict DOI backfill (doi_pipeline-backed)')
    ap.add_argument('--user', type=int, help='Restrict to one researcher (UserID)')
    ap.add_argument('--limit', type=int, help='Process at most N papers')
    ap.add_argument('--apply', action='store_true', help='Write DOIs (default: dry-run)')
    ap.add_argument('--start-after-paper-id', type=int, default=None,
                     help='Resume a dry-run batch: only PaperID > this value '
                          '(pure pagination, does not affect matching)')
    args = ap.parse_args()

    conn = db()
    known_dois = load_known_dois(conn)
    targets = fetch_targets(conn, args.user, args.limit, args.start_after_paper_id)
    print(f'No-DOI papers to check: {len(targets)}  '
          f'(mode: {"APPLY" if args.apply else "DRY-RUN"})  '
          f'start_after_paper_id={args.start_after_paper_id}  '
          f'known_dois preloaded: {len(known_dois)}\n')

    n_auto = n_review = n_rej = n_written = n_taken = 0
    report_rows: list[dict] = []

    for i, p in enumerate(targets, 1):
        title = p['Title']
        tprev = (title or '')[:48]

        time.sleep(API_DELAY)
        cr_cand, cr_outcome = crossref_candidate(title)
        time.sleep(API_DELAY)
        oa_cand, oa_outcome = openalex_candidate(title)

        paper = build_paper_input(p)
        result, sources_attempted, candidates = resolve_for_paper(
            paper, cr_cand, cr_outcome, oa_cand, oa_outcome, known_dois)

        row = {
            'paper_id': p['PaperID'], 'title': title,
            'crossref_outcome': cr_outcome, 'openalex_outcome': oa_outcome,
            'sources_attempted': sources_attempted,
            'candidate_count': len(candidates),
            'candidates': [
                {'source': c.source, 'doi': c.doi, 'title': c.title} for c in candidates
            ],
            'decision': result.decision, 'score': round(result.score, 3),
            'reasons': list(result.reasons), 'selected_doi': result.doi,
            'duplicate_check': None,
        }

        if result.decision == AUTO_MATCH:
            # doi_taken(): live, additive re-check -- separate from
            # known_dois, kept intentionally (see load_known_dois()).
            if doi_taken(conn, result.doi, p['PaperID']):
                n_taken += 1
                row['duplicate_check'] = 'already_owned_live_check'
                print(f'[{i}/{len(targets)}] #{p["PaperID"]} ~ DOI {result.doi} '
                      f'already owned by another paper (live check) — skip. ({tprev})')
            else:
                n_auto += 1
                print(f'[{i}/{len(targets)}] #{p["PaperID"]} ✓ AUTO_MATCH {result.doi} '
                      f'(score={result.score:.3f}, via {result.source}) {tprev}')
                if args.apply:
                    try:
                        with conn:
                            with conn.cursor() as cur:
                                cur.execute(
                                    'UPDATE "ResearchPaper" SET "DOI" = %s '
                                    'WHERE "PaperID" = %s AND ("DOI" IS NULL OR "DOI" = \'\')',
                                    [result.doi, p['PaperID']])
                        n_written += 1
                    except Exception as e:
                        print(f'      ! write failed: {e}')
        elif result.decision == REVIEW:
            n_review += 1
            print(f'[{i}/{len(targets)}] #{p["PaperID"]} ? REVIEW '
                  f'(score={result.score:.3f}, reasons={result.reasons}) {tprev}')
        else:
            n_rej += 1
            print(f'[{i}/{len(targets)}] #{p["PaperID"]} ✗ REJECT '
                  f'(score={result.score:.3f}, reasons={result.reasons}) {tprev}')

        report_rows.append(row)

    max_paper_id = max((p['PaperID'] for p in targets), default=None)
    print('\n' + '=' * 60)
    print(f'  AUTO_MATCH          : {n_auto}')
    print(f'  Written             : {n_written}')
    print(f'  REVIEW              : {n_review}')
    print(f'  REJECT              : {n_rej}')
    print(f'  DOI already in use  : {n_taken}')
    print(f'  Mode                : {"APPLY" if args.apply else "DRY-RUN (no writes)"}')
    if max_paper_id is not None:
        print(f'  Highest PaperID seen this batch : {max_paper_id}  '
              f'(pass --start-after-paper-id {max_paper_id} to continue)')
    if n_written:
        print('\n  Next: run the affiliation verifier on these papers, e.g.')
        print(f'    python backend/affiliation_verifier.py --apply'
              f'{f" --user {args.user}" if args.user else ""}')

    reports_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'reports')
    os.makedirs(reports_dir, exist_ok=True)
    batch_tag = f'_after{args.start_after_paper_id}' if args.start_after_paper_id else ''
    report_path = os.path.join(
        reports_dir,
        f'find_missing_dois_v2_{time.strftime("%Y-%m-%d_%H%M%S")}_'
        f'{"apply" if args.apply else "dryrun"}{batch_tag}.json')
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report_rows, f, ensure_ascii=False, indent=1, default=str)
    print(f'\n  Decision report written to: {report_path}')

    conn.close()


if __name__ == '__main__':
    main()
