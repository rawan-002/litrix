"""Force-link a researcher's Scholar papers to their UserID.

Why this exists:
  When `litrix_scraper.py` finds a paper that was already in the DB
  (status=existing), in some flows it skips creating the Authors row,
  leaving the researcher with 0 papers attributed.
  This script re-fetches the Scholar profile and ensures every paper
  the researcher published is linked in the Authors table.

Usage:
  set DATABASE_URL=postgresql://...   (point at Neon or local)
  python link_scholar_papers.py <scholar_id> <user_id>
"""
import os
import sys
import time
from dotenv import load_dotenv
import psycopg2
import httpx
from serpapi import GoogleSearch

load_dotenv()

SERP_KEY = os.getenv("SERP_API_KEY")
if not SERP_KEY:
    print("Missing SERP_API_KEY")
    sys.exit(1)


def db():
    url = os.getenv("DATABASE_URL")
    if url:
        return psycopg2.connect(url)
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "LitrixDB"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
    )


def fetch_scholar_profile(scholar_id):
    """
    Pull the full Scholar profile via SerpAPI:
      - articles      : list of papers with cited_by.value
      - cited_by_graph: list of {year, citations} for the author overall
    """
    papers = []
    cited_by_graph = []
    start = 0
    while True:
        r = GoogleSearch({
            "engine": "google_scholar_author",
            "author_id": scholar_id,
            "api_key": SERP_KEY,
            "num": 100,
            "start": start,
        }).get_dict()
        if "error" in r:
            print(f"SerpAPI error: {r['error']}")
            break
        # cited_by.graph is on the FIRST page only
        if start == 0:
            cb = r.get("cited_by") or {}
            cited_by_graph = cb.get("graph") or []
        articles = r.get("articles", []) or []
        if not articles:
            break
        papers.extend(articles)
        if len(articles) < 100:
            break
        start += 100
    return papers, cited_by_graph


def normalize_title(t):
    return ''.join(c for c in (t or '').lower() if c.isalnum() or c.isspace()).strip()


def openalex_lookup_by_title(title, author_keywords=None):
    """
    Find a paper on OpenAlex by title — STRICT matching:
      1. Title must EXACTLY normalize-match
      2. AND at least one author name must contain one of author_keywords
         (e.g. 'fathy' or 'mohammed') to prevent matching popular papers
         by completely different authors with similar titles.

    Returns the matched work dict or None.
    """
    try:
        r = httpx.get(
            "https://api.openalex.org/works",
            params={"search": title, "per-page": 5, "mailto": "ra20awn@gmail.com"},
            timeout=20,
        )
        r.raise_for_status()
        results = r.json().get("results", []) or []
    except Exception:
        return None
    n_target = normalize_title(title)
    keywords = [k.lower() for k in (author_keywords or []) if k]

    for w in results:
        # 1. Title must match exactly (normalized)
        if normalize_title(w.get("title", "")) != n_target:
            continue
        # 2. If keywords given, at least one author must include one
        if keywords:
            authors_blob = ' '.join(
                (ship.get("author", {}).get("display_name") or '').lower()
                for ship in (w.get("authorships") or [])
            )
            if not any(k in authors_blob for k in keywords):
                continue
        return w
    return None


def enrich_paper(cur, paper_id, title, author_keywords):
    """
    Pull counts_by_year + DOI + journal info from OpenAlex; UPDATE the paper.
    Strict author validation: only enriches if the OpenAlex match has an
    author whose name contains one of `author_keywords`.
    """
    from psycopg2.extras import Json
    oa = openalex_lookup_by_title(title, author_keywords=author_keywords)
    if not oa:
        return False
    doi = (oa.get("doi") or "").replace("https://doi.org/", "").lower() or None
    cby_list = oa.get("counts_by_year") or []
    counts_by_year = {
        str(item["year"]): int(item.get("cited_by_count") or 0)
        for item in cby_list
        if item.get("year") is not None
    } or None
    cited_by = oa.get("cited_by_count")

    # Try to extract journal info from OpenAlex
    primary_loc = oa.get("primary_location") or {}
    source = primary_loc.get("source") or {}
    journal_name = source.get("display_name")
    issn = source.get("issn_l")
    venue_type = 'Conference' if (source.get("type") == 'conference') else 'Journal'

    journal_id = None
    if journal_name:
        # Find or create journal
        if issn:
            cur.execute('SELECT "JournalID" FROM "Journals" WHERE "ISSN_Print" = %s', (issn,))
            r = cur.fetchone()
            if r:
                journal_id = r[0]
        if not journal_id:
            cur.execute('SELECT "JournalID" FROM "Journals" WHERE LOWER("JournalName") = LOWER(%s) LIMIT 1', (journal_name,))
            r = cur.fetchone()
            if r:
                journal_id = r[0]
        if not journal_id:
            cur.execute('''
                INSERT INTO "Journals" ("JournalName", "ISSN_Print", "VenueType")
                VALUES (%s, %s, %s) RETURNING "JournalID"
            ''', (journal_name, issn, venue_type))
            journal_id = cur.fetchone()[0]

    cur.execute('''
        UPDATE "ResearchPaper"
        SET "DOI"             = COALESCE("DOI", %s),
            "JournalID"       = COALESCE("JournalID", %s),
            "CitationsByYear" = COALESCE(%s, "CitationsByYear"),
            "RawData_Log" = jsonb_set(
                COALESCE("RawData_Log", '{}'::jsonb),
                '{cited_by_count}',
                to_jsonb(%s::int)
            )
        WHERE "PaperID" = %s
    ''', (doi, journal_id, Json(counts_by_year) if counts_by_year else None, cited_by or 0, paper_id))
    return True


def main():
    if len(sys.argv) < 3:
        print("Usage: python link_scholar_papers.py <scholar_id> <user_id>")
        sys.exit(1)
    scholar_id = sys.argv[1]
    user_id = int(sys.argv[2])

    print(f"Fetching Scholar profile {scholar_id}...")
    articles, cited_by_graph = fetch_scholar_profile(scholar_id)
    print(f"Got {len(articles)} articles, {len(cited_by_graph)} year-citation points")

    conn = db()
    cur = conn.cursor()

    cur.execute('''
        SELECT "UserID", "FullName_Ar", "FirstName", "LastName"
        FROM "Users" WHERE "UserID" = %s
    ''', (user_id,))
    u = cur.fetchone()
    if not u:
        print(f"UserID {user_id} not found")
        sys.exit(1)
    print(f"Researcher: {u[1]}")

    # Author keywords: parts of the researcher's name to validate OpenAlex
    # matches against. We extract English-transliterable name parts from
    # FirstName/LastName + the first article's authors string.
    author_keywords = []
    for part in [u[2], u[3]]:
        if part:
            for token in str(part).split():
                t = ''.join(c for c in token.lower() if c.isalpha())
                if len(t) >= 4:
                    author_keywords.append(t)
    # Also add tokens from the Scholar 'authors' string of the first article
    if articles:
        a0_authors = (articles[0].get("authors") or "").lower()
        for token in a0_authors.replace(',', ' ').split():
            t = ''.join(c for c in token if c.isalpha())
            if len(t) >= 4 and t not in author_keywords:
                author_keywords.append(t)
    # De-dupe + normalize
    author_keywords = list(set(author_keywords))
    print(f"Author validation keywords: {author_keywords}")

    n_linked = 0
    n_skipped = 0
    n_notfound = 0
    for a in articles:
        title = (a.get("title") or "").strip()
        if not title:
            continue
        norm = normalize_title(title)

        # Try exact title match first
        cur.execute(
            'SELECT "PaperID", "Title" FROM "ResearchPaper" '
            'WHERE LOWER("Title") = LOWER(%s) '
            'OR "NormalizedTitle" = %s '
            'LIMIT 1',
            (title, norm),
        )
        row = cur.fetchone()
        if not row:
            # Insert the paper. Pull cited_by counts from RawData if present.
            from psycopg2.extras import Json
            year = a.get("year")
            try:
                year = int(year) if year else None
            except (ValueError, TypeError):
                year = None

            cited_by_count = 0
            try:
                cb = a.get("cited_by") or {}
                cited_by_count = int(cb.get("value") or 0)
            except (ValueError, TypeError):
                cited_by_count = 0

            # Enrich RawData_Log with a cited_by_count alias the dashboard
            # SQL already knows how to read.
            raw_log = dict(a)
            raw_log['cited_by_count'] = cited_by_count

            cur.execute('''
                INSERT INTO "ResearchPaper" (
                    "Title", "NormalizedTitle", "PubYear",
                    "Source", "RawData_Log", "ScrapedAt",
                    "IsVerified"
                )
                VALUES (%s, %s, %s, 'Scholar', %s, NOW(), FALSE)
                RETURNING "PaperID"
            ''', (title, norm, year, Json(raw_log)))
            paper_id = cur.fetchone()[0]
            print(f"  + INSERTED PaperID={paper_id}: {title[:60]}")
            n_notfound += 1
            # Enrich from OpenAlex (DOI + citations_by_year)
            time.sleep(0.15)  # be polite to OpenAlex API
            if enrich_paper(cur, paper_id, title, author_keywords):
                print(f"      ↳ enriched from OpenAlex")
        else:
            paper_id = row[0]
            # If existing paper has no CitationsByYear, enrich it now too
            cur.execute('SELECT "CitationsByYear" FROM "ResearchPaper" WHERE "PaperID" = %s', (paper_id,))
            r = cur.fetchone()
            if r and (r[0] is None or r[0] == {}):
                time.sleep(0.15)
                if enrich_paper(cur, paper_id, title, author_keywords):
                    print(f"      ↳ enriched existing PaperID={paper_id} from OpenAlex")

        # Check if already linked
        cur.execute(
            'SELECT 1 FROM "Authors" WHERE "UserID" = %s AND "PaperID" = %s',
            (user_id, paper_id),
        )
        if cur.fetchone():
            n_skipped += 1
            continue

        # Link it
        cur.execute('''
            INSERT INTO "Authors" (
                "UserID", "PaperID", "AuthorOrder",
                "IsCorrespondingAuthor", "MappingConfidence", "MappingCriteria",
                "AuthorNameRaw", "Is_Verified"
            )
            VALUES (%s, %s, NULL, FALSE, 1.0, 'manual_scholar_link', %s, TRUE)
            ON CONFLICT ("UserID", "PaperID") DO NOTHING
        ''', (user_id, paper_id, a.get("authors")))
        n_linked += 1
        print(f"  + linked PaperID={paper_id}: {title[:60]}")

    # Save the author-level per-year citations from Scholar.
    # This is what Scholar shows in the profile graph — most accurate.
    from psycopg2.extras import Json
    cby_dict = {
        str(item.get("year")): int(item.get("citations") or 0)
        for item in cited_by_graph
        if item.get("year") is not None
    } or None

    cur.execute('''
        UPDATE "Researcher"
        SET "CitationsByYear" = %s,
            "LastSyncedAt"    = NOW()
        WHERE "UserID" = %s
    ''', (Json(cby_dict) if cby_dict else None, user_id))
    if cur.rowcount == 0:
        # Create Researcher row if missing
        cur.execute('''
            INSERT INTO "Researcher" ("UserID", "CitationsByYear", "LastSyncedAt")
            VALUES (%s, %s, NOW())
            ON CONFLICT ("UserID") DO UPDATE
            SET "CitationsByYear" = EXCLUDED."CitationsByYear",
                "LastSyncedAt"    = EXCLUDED."LastSyncedAt"
        ''', (user_id, Json(cby_dict) if cby_dict else None))
    print(f"  ✓ saved Scholar citations-per-year ({len(cby_dict or {})} years)")

    conn.commit()
    print(f"\nDone — linked={n_linked}, already-linked={n_skipped}, inserted-and-linked={n_notfound}")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
