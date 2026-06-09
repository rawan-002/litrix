"""
Incremental Scholar scraper — NEW papers only.

=============================================================================
WHY THIS EXISTS (user requirement)
=============================================================================
The full scraper (scrapers/scholar.py) re-fetches a researcher's ENTIRE
Scholar profile in 3 passes — correct for first-time sync, wasteful for
keeping profiles fresh. This script does the opposite:

    For each researcher:
      1. cutoff = MAX(PubYear) of the papers already linked to them in the DB
      2. Fetch their Scholar profile sorted by PUBDATE (newest first)
      3. Walk the list; the moment we hit a paper OLDER than the cutoff,
         STOP — everything after it is older still (sorted), so no further
         pages and no further SerpAPI credits.
      4. Insert only the genuinely-new papers (same dedup + enrichment +
         author-link logic as scholar.py, so nothing existing is duplicated
         and a paper a co-author already has just gets LINKED, not re-added).

WHAT IT NEVER DOES
    • Never re-downloads or rewrites existing papers (dedup check first;
      enrichment uses COALESCE so it only fills NULL DOI/JournalID).
    • Never deletes anything.
    • Never touches Researcher.CitationsByYear or LastSyncedAt — so the
      7-day full-sync cooldown is unaffected by incremental runs.

CREDITS: 1 SerpAPI call per page (100 articles). A researcher with no new
papers costs exactly 1 call. --budget hard-caps the total calls per run.

USAGE:
    python scrapers/scholar_new_papers.py --dry-run            # report only
    python scrapers/scholar_new_papers.py --apply              # all researchers
    python scrapers/scholar_new_papers.py --apply --user 106   # one researcher
    python scrapers/scholar_new_papers.py --apply --budget 80
"""
import sys
import time
import argparse
from pathlib import Path

import psycopg2
from psycopg2.extras import Json

# Reuse the battle-tested helpers from the full scraper (db switch, title
# normalization, SerpAPI paging, OpenAlex enrichment, journal matching).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from scholar import (  # noqa: E402
    db, normalize_title, serp_page, openalex_enrich, find_journal_id,
)

MAX_PAGES_PER_RESEARCHER = 3   # 300 newest articles — plenty for "new only"


def latest_pub_year(cur, user_id):
    """Year of the researcher's most recent stored paper (None = no papers)."""
    cur.execute('''
        SELECT MAX(rp."PubYear")
        FROM "ResearchPaper" rp
        JOIN "Authors" a ON a."PaperID" = rp."PaperID"
        WHERE a."UserID" = %s
    ''', (user_id,))
    row = cur.fetchone()
    return row[0] if row else None


def fetch_new_articles(scholar_id, cutoff_year, budget_left):
    """Newest-first fetch that STOPS at the first article older than cutoff.

    Returns (articles, calls_used, hit_cutoff).
    Articles with no parseable year are skipped — "new only" means we never
    guess; undated entries are left for a full re-sync to handle.
    """
    new_articles = []
    calls = 0
    start = 0
    for _ in range(MAX_PAGES_PER_RESEARCHER):
        if calls >= budget_left:
            return new_articles, calls, False
        articles, _raw = serp_page(scholar_id, start, sort="pubdate")
        calls += 1
        if articles is None or not articles:
            return new_articles, calls, True
        for a in articles:
            year = a.get("year")
            try:
                year = int(year) if year else None
            except (ValueError, TypeError):
                year = None
            if year is None:
                continue                      # undated → not provably new
            if year < cutoff_year:
                return new_articles, calls, True   # sorted: rest is older
            new_articles.append(a)
        if len(articles) < 100:
            return new_articles, calls, True
        start += 100
        time.sleep(0.7)
    return new_articles, calls, True


def process_article(cur, a, user_id):
    """Insert-or-link one Scholar article (ported from scholar.py main loop).

    Returns 'inserted' | 'linked_existing' | 'already_linked' | 'skipped'.
    """
    title = (a.get("title") or "").strip()
    if not title:
        return 'skipped'
    norm = normalize_title(title)
    scholar_authors_str = a.get("authors") or ""

    cur.execute(
        'SELECT "PaperID", "DOI", "JournalID" FROM "ResearchPaper" '
        'WHERE "NormalizedTitle" = %s OR LOWER("Title") = LOWER(%s) LIMIT 1',
        (norm, title),
    )
    existing = cur.fetchone()

    if existing:
        paper_id, existing_doi, existing_journal = existing
        was_new = False
    else:
        year = a.get("year")
        try:
            year = int(year) if year else None
        except (ValueError, TypeError):
            year = None
        cited_by_count = 0
        try:
            cited_by_count = int((a.get("cited_by") or {}).get("value") or 0)
        except (ValueError, TypeError):
            pass
        raw_log = dict(a)
        raw_log['cited_by_count'] = cited_by_count
        try:
            cur.execute('SAVEPOINT before_insert')
            cur.execute('''
                INSERT INTO "ResearchPaper" (
                    "Title", "NormalizedTitle", "PubYear",
                    "Source", "RawData_Log", "ScrapedAt", "IsVerified"
                )
                VALUES (%s, %s, %s, 'Scholar', %s, NOW(), TRUE)
                RETURNING "PaperID"
            ''', (title, norm, year, Json(raw_log)))
            paper_id = cur.fetchone()[0]
            cur.execute('RELEASE SAVEPOINT before_insert')
            existing_doi = existing_journal = None
            was_new = True
        except psycopg2.errors.UniqueViolation:
            cur.execute('ROLLBACK TO SAVEPOINT before_insert')
            cur.execute(
                'SELECT "PaperID", "DOI", "JournalID" FROM "ResearchPaper" '
                'WHERE LOWER("Title") = LOWER(%s) LIMIT 1',
                (title,),
            )
            r = cur.fetchone()
            if not r:
                return 'skipped'
            paper_id, existing_doi, existing_journal = r
            was_new = False

    # Enrich ONLY missing fields (COALESCE — never overwrites existing data)
    if not existing_doi or not existing_journal:
        time.sleep(0.15)
        doi, journal_name, issn = openalex_enrich(title, scholar_authors_str)
        if doi or journal_name:
            final_doi = doi
            if final_doi:
                cur.execute(
                    'SELECT "PaperID" FROM "ResearchPaper" '
                    'WHERE LOWER("DOI") = %s AND "PaperID" <> %s',
                    (final_doi.lower(), paper_id),
                )
                if cur.fetchone():
                    final_doi = None
            jid = find_journal_id(cur, issn, journal_name)
            cur.execute('''
                UPDATE "ResearchPaper"
                SET "DOI"       = COALESCE("DOI", %s),
                    "JournalID" = COALESCE("JournalID", %s)
                WHERE "PaperID" = %s
            ''', (final_doi, jid, paper_id))

    cur.execute('''
        INSERT INTO "Authors" (
            "UserID", "PaperID", "AuthorOrder",
            "IsCorrespondingAuthor", "MappingConfidence", "MappingCriteria",
            "AuthorNameRaw", "Is_Verified"
        )
        VALUES (%s, %s, NULL, FALSE, 1.0, 'scholar_id_verified', %s, TRUE)
        ON CONFLICT ("UserID", "PaperID") DO NOTHING
    ''', (user_id, paper_id, scholar_authors_str))
    linked_now = cur.rowcount > 0

    if was_new:
        return 'inserted'
    return 'linked_existing' if linked_now else 'already_linked'


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="Fetch + report what WOULD be inserted (uses SerpAPI "
                           "credits for the fetch, but writes nothing).")
    mode.add_argument("--apply", action="store_true",
                      help="Insert new papers + author links.")
    ap.add_argument("--user", type=int, default=None,
                    help="One researcher only (UserID).")
    ap.add_argument("--budget", type=int, default=100,
                    help="HARD cap on SerpAPI calls this run (default 100; "
                         "a researcher with no new papers costs 1 call).")
    args = ap.parse_args()

    conn = db()
    cur = conn.cursor()

    where = 'u."Scholar_ID" IS NOT NULL AND u."Scholar_ID" <> \'\''
    params = []
    if args.user:
        where += ' AND u."UserID" = %s'
        params.append(args.user)
    cur.execute(f'''
        SELECT u."UserID", u."FullName_Ar", u."Scholar_ID"
        FROM "Users" u
        WHERE {where}
        ORDER BY u."UserID"
    ''', params)
    researchers = cur.fetchall()
    print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'} | "
          f"researchers with Scholar_ID: {len(researchers)} | "
          f"SerpAPI budget: {args.budget}\n")

    totals = {'inserted': 0, 'linked_existing': 0, 'already_linked': 0,
              'skipped': 0, 'no_papers_skipped': 0, 'calls': 0}

    for user_id, name, scholar_id in researchers:
        if totals['calls'] >= args.budget:
            print(f"\n[BUDGET] {args.budget} SerpAPI calls used — stopping. "
                  f"Re-run to continue with the remaining researchers.")
            break

        cutoff = latest_pub_year(cur, user_id)
        if cutoff is None:
            # No stored papers → nothing to be "newer than". A first-time
            # researcher needs the FULL sync (admin/sync table), not this.
            totals['no_papers_skipped'] += 1
            print(f"[skip] {name} (UID {user_id}) — no stored papers; "
                  f"use the full Sync button instead")
            continue

        articles, calls, completed = fetch_new_articles(
            scholar_id, cutoff, args.budget - totals['calls'])
        totals['calls'] += calls

        if not articles:
            print(f"[ok]   {name} (UID {user_id}) — up to date "
                  f"(latest stored year {cutoff}, {calls} call)")
            continue

        print(f"[new]  {name} (UID {user_id}) — {len(articles)} candidate(s) "
              f"with year >= {cutoff}:")
        for a in articles:
            title = (a.get("title") or "").strip()
            if args.apply:
                status = process_article(cur, a, user_id)
                totals[status] += 1
                print(f"        {status:<16} [{a.get('year')}] {title[:70]}")
            else:
                print(f"        candidate        [{a.get('year')}] {title[:70]}")
        if args.apply:
            conn.commit()

    print(f"\n{'=' * 70}")
    print(f"  SerpAPI calls used   : {totals['calls']}/{args.budget}")
    if args.apply:
        print(f"  New papers inserted  : {totals['inserted']}")
        print(f"  Linked to existing   : {totals['linked_existing']}")
        print(f"  Already linked       : {totals['already_linked']}")
        print(f"  Skipped (no title)   : {totals['skipped']}")
    print(f"  Researchers w/o papers: {totals['no_papers_skipped']} "
          f"(need full sync first)")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
