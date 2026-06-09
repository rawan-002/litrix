"""
============================================================================
STRICT DOI BACKFILL
============================================================================
Finds the canonical DOI for ResearchPaper rows that have none, so the
affiliation verifier (which is DOI-driven) can then process them.

WHY A SEPARATE, STRICTER TOOL
-----------------------------
The existing `manage.py backfill_dois` uses analytics.lookups.paper_by_title
with a LOW title-similarity threshold (0.72) and NO year/author check. On
common or generic titles that silently attaches the WRONG paper's DOI — the
exact failure mode behind the ESPR / MJSAT mix-ups we found by hand.

This tool only writes a DOI when ALL THREE hold:
    1. normalized title similarity >= 0.90  (near-exact)
    2. publication year within +-1 of ours  (print/online drift)
    3. at least one author surname from our record also appears in the
       candidate's author list (when we have an author string to compare)

Anything weaker is REJECTED and left for a human — never guessed.

SAFETY
------
* Dry-run by default; --apply required to write.
* Per-paper transaction; never marks/overwrites an existing DOI.
* Refuses a DOI already owned by another paper in the DB.
* Idempotent: re-running only revisits still-DOI-less rows.

USAGE
-----
    python backend/find_missing_dois.py --user 1            # dry-run, one researcher
    python backend/find_missing_dois.py --user 1 --apply    # write the strong matches
    python backend/find_missing_dois.py --apply --limit 50  # whole DB, capped
============================================================================
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys
import time
from difflib import SequenceMatcher
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

# --- strict thresholds ------------------------------------------------------
TITLE_SIM_MIN  = 0.90   # near-exact title
YEAR_TOLERANCE = 1      # allow print/online year drift
API_DELAY      = 0.15


def db():
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL not set (.env).")
        sys.exit(1)
    return psycopg2.connect(DATABASE_URL, connect_timeout=15)


_WS = re.compile(r'\s+')
_NON = re.compile(r'[^\w\s]')


def norm(s: str) -> str:
    return _WS.sub(' ', _NON.sub(' ', (s or '').lower())).strip()


def title_sim(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


# Latin surname tokens (>=3 chars) from a raw author string like
# "A Alzahrani, V Callaghan, M Gardner". Crude but effective for overlap.
def surnames(raw: str) -> set[str]:
    out = set()
    for tok in re.findall(r'[A-Za-zÀ-ɏ]{3,}', raw or ''):
        out.add(tok.lower())
    return out


def openalex_candidates(title: str) -> list[dict]:
    url = 'https://api.openalex.org/works'
    params = {'search': title, 'per-page': 5,
              'select': 'id,doi,title,publication_year,authorships'}
    try:
        r = requests.get(url, params=params, headers={'User-Agent': UA}, timeout=25)
        if r.status_code != 200:
            return []
        return r.json().get('results', []) or []
    except (requests.RequestException, ValueError):
        return []


def evaluate(paper: dict) -> dict:
    """
    Returns {'doi','score','year','cand_title','reason','accept'} for the best
    candidate, or accept=False with a reason.
    """
    our_title = paper['Title']
    our_year  = paper['PubYear']
    our_names = surnames(paper.get('AuthorNameRaw') or '')

    best = None
    for c in openalex_candidates(our_title):
        doi = (c.get('doi') or '').replace('https://doi.org/', '').strip()
        if not doi:
            continue
        sim = title_sim(our_title, c.get('title') or '')
        cand_names = set()
        for a in c.get('authorships', []):
            cand_names |= surnames((a.get('author') or {}).get('display_name', ''))
        rec = {
            'doi': doi, 'score': round(sim, 3),
            'year': c.get('publication_year'),
            'cand_title': c.get('title') or '',
            'author_overlap': bool(our_names & cand_names) if our_names else None,
        }
        if best is None or sim > best['score']:
            best = rec

    if not best:
        return {'accept': False, 'reason': 'no_candidate_with_doi'}

    # Strict gate
    if best['score'] < TITLE_SIM_MIN:
        return {**best, 'accept': False, 'reason': f'title_sim<{TITLE_SIM_MIN}'}
    if our_year and best['year'] and abs(int(best['year']) - int(our_year)) > YEAR_TOLERANCE:
        return {**best, 'accept': False, 'reason': 'year_mismatch'}
    if our_names and best['author_overlap'] is False:
        return {**best, 'accept': False, 'reason': 'no_author_overlap'}

    return {**best, 'accept': True, 'reason': 'ok'}


def fetch_targets(conn, user_id, limit):
    where = ['(rp."DOI" IS NULL OR rp."DOI" = \'\')',
             'rp."Title" IS NOT NULL', 'LENGTH(rp."Title") >= 12']
    params: list[Any] = []
    if user_id is not None:
        where.append('EXISTS (SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID" '
                     'AND a."UserID" = %s)')
        params.append(user_id)
    sql = f'''
        SELECT rp."PaperID", rp."Title", rp."PubYear",
               (SELECT a."AuthorNameRaw" FROM "Authors" a
                WHERE a."PaperID" = rp."PaperID" AND a."AuthorNameRaw" IS NOT NULL
                LIMIT 1) AS "AuthorNameRaw"
        FROM "ResearchPaper" rp
        WHERE {' AND '.join(where)}
        ORDER BY rp."PaperID"
        {f'LIMIT {int(limit)}' if limit else ''}
    '''
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def doi_taken(conn, doi, paper_id) -> bool:
    with conn.cursor() as cur:
        cur.execute('SELECT 1 FROM "ResearchPaper" WHERE LOWER("DOI") = LOWER(%s) '
                    'AND "PaperID" <> %s LIMIT 1', [doi, paper_id])
        return cur.fetchone() is not None


def main():
    ap = argparse.ArgumentParser(description='Strict DOI backfill')
    ap.add_argument('--user', type=int, help='Restrict to one researcher (UserID)')
    ap.add_argument('--limit', type=int, help='Process at most N papers')
    ap.add_argument('--apply', action='store_true', help='Write DOIs (default: dry-run)')
    args = ap.parse_args()

    conn = db()
    targets = fetch_targets(conn, args.user, args.limit)
    print(f'No-DOI papers to check: {len(targets)}  '
          f'(mode: {"APPLY" if args.apply else "DRY-RUN"})\n')

    n_ok = n_rej = n_written = n_taken = 0
    for i, p in enumerate(targets, 1):
        time.sleep(API_DELAY)
        res = evaluate(p)
        tprev = (p['Title'] or '')[:48]
        if res.get('accept'):
            if doi_taken(conn, res['doi'], p['PaperID']):
                n_taken += 1
                print(f'[{i}/{len(targets)}] #{p["PaperID"]} ~ DOI {res["doi"]} '
                      f'already owned by another paper — skip. ({tprev})')
                continue
            n_ok += 1
            print(f'[{i}/{len(targets)}] #{p["PaperID"]} ✓ {res["doi"]} '
                  f'(sim={res["score"]}, yr={res["year"]}) {tprev}')
            if args.apply:
                try:
                    with conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                'UPDATE "ResearchPaper" SET "DOI" = %s '
                                'WHERE "PaperID" = %s AND ("DOI" IS NULL OR "DOI" = \'\')',
                                [res['doi'], p['PaperID']])
                    n_written += 1
                except Exception as e:
                    print(f'      ! write failed: {e}')
        else:
            n_rej += 1
            extra = f'sim={res.get("score")}' if 'score' in res else ''
            print(f'[{i}/{len(targets)}] #{p["PaperID"]} ✗ rejected '
                  f'({res["reason"]} {extra}) {tprev}')

    print('\n' + '=' * 60)
    print(f'  Strong DOI matches : {n_ok}')
    print(f'  Written            : {n_written}')
    print(f'  Rejected (weak)    : {n_rej}')
    print(f'  DOI already in use : {n_taken}')
    print(f'  Mode               : {"APPLY" if args.apply else "DRY-RUN (no writes)"}')
    if n_written:
        print('\n  Next: run the affiliation verifier on these papers, e.g.')
        print(f'    python backend/affiliation_verifier.py --apply'
              f'{f" --user {args.user}" if args.user else ""}')
    conn.close()


if __name__ == '__main__':
    main()
