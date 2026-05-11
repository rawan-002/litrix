import os
import sys
import time
import io
import re
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import Json
from serpapi import GoogleSearch
import httpx

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)
except Exception:
    pass

load_dotenv()
SERP_KEY = os.getenv("SERP_API_KEY")
if not SERP_KEY:
    print("Missing SERP_API_KEY")
    sys.exit(1)


def db():
    keepalive_kwargs = dict(
        keepalives=1, keepalives_idle=30,
        keepalives_interval=10, keepalives_count=5,
    )
    url = os.getenv("DATABASE_URL")
    if url:
        return psycopg2.connect(url, **keepalive_kwargs)
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "LitrixDB"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
        **keepalive_kwargs,
    )


def normalize_title(t):
    return ''.join(c for c in (t or '').lower() if c.isalnum() or c.isspace()).strip()


def normalize_issn(issn):
    if not issn:
        return None
    return ''.join(c for c in str(issn).upper() if c.isalnum())


def extract_lastnames(authors_str):
    if not authors_str:
        return set()
    out = set()
    for chunk in authors_str.split(','):
        tokens = chunk.strip().split()
        if not tokens:
            continue
        last = ''.join(c for c in tokens[-1].lower() if c.isalpha())
        if len(last) >= 4:
            out.add(last)
    return out


def serp_page(scholar_id, start, sort=None):
    for _ in range(5):
        try:
            params = {
                "engine": "google_scholar_author",
                "author_id": scholar_id,
                "api_key": SERP_KEY,
                "num": 100,
                "start": start,
            }
            if sort:
                params["sort"] = sort
            r = GoogleSearch(params).get_dict()
        except Exception:
            time.sleep(2)
            continue
        if "error" in r:
            return None, None
        return r.get("articles", []) or [], r
    return None, None


def fetch_scholar_profile(scholar_id):
    all_articles = {}
    cited_by_graph = []
    for sort in [None, "pubdate", "title"]:
        start = 0
        empty_streak = 0
        pages = 0
        while True:
            articles, raw = serp_page(scholar_id, start, sort=sort)
            if articles is None:
                break
            if not cited_by_graph and raw:
                cb = raw.get("cited_by") or {}
                cited_by_graph = cb.get("graph") or []
            if not articles:
                empty_streak += 1
                if empty_streak >= 3:
                    break
                start += 100
                time.sleep(1.5)
                continue
            empty_streak = 0
            for a in articles:
                t = (a.get("title") or "").strip()
                if not t:
                    continue
                key = (
                    a.get("cites_id")
                    or a.get("citation_id")
                    or a.get("link")
                    or normalize_title(t)
                )
                all_articles.setdefault(key, a)
            pages += 1
            if len(articles) < 100:
                break
            start += 100
            time.sleep(0.7)
            if pages >= 30:
                break
    return list(all_articles.values()), cited_by_graph


def openalex_enrich(title, scholar_authors_str):
    n_target = normalize_title(title)
    if len(n_target) < 15:
        return None, None, None
    lastnames = extract_lastnames(scholar_authors_str)
    if not lastnames:
        return None, None, None
    try:
        r = httpx.get(
            "https://api.openalex.org/works",
            params={"search": title, "per-page": 5, "mailto": "ra20awn@gmail.com"},
            timeout=20,
        )
        r.raise_for_status()
        results = r.json().get("results", []) or []
    except Exception:
        return None, None, None
    for w in results:
        if normalize_title(w.get("title") or "") != n_target:
            continue
        oa_authors = ' '.join(
            (ship.get("author", {}).get("display_name") or '').lower()
            for ship in (w.get("authorships") or [])
        )
        oa_authors = ''.join(c if c.isalpha() else ' ' for c in oa_authors)
        if not any(ln in oa_authors for ln in lastnames):
            continue
        doi = (w.get("doi") or "").replace("https://doi.org/", "").lower() or None
        primary_loc = w.get("primary_location") or {}
        source = primary_loc.get("source") or {}
        return doi, source.get("display_name"), source.get("issn_l")
    return None, None, None


def find_journal_id(cur, issn, journal_name):
    issn_norm = normalize_issn(issn)
    if issn_norm:
        cur.execute('SELECT "JournalID" FROM "Journals" WHERE "ISSN_Print" = %s LIMIT 1', (issn_norm,))
        r = cur.fetchone()
        if r:
            return r[0]
        cur.execute('SELECT "JournalID" FROM "JournalRankings" WHERE "Issn" = %s LIMIT 1', (issn_norm,))
        r = cur.fetchone()
        if r and r[0]:
            jid = r[0]
            cur.execute(
                'UPDATE "Journals" SET "ISSN_Print" = COALESCE("ISSN_Print", %s) WHERE "JournalID" = %s',
                (issn_norm, jid),
            )
            return jid
        cur.execute('SELECT "RankingID" FROM "ISSN_Mapping" WHERE "ISSN" = %s LIMIT 1', (issn_norm,))
        r = cur.fetchone()
        if r:
            cur.execute(
                'SELECT "JournalID" FROM "JournalRankings" WHERE "RankingID" = %s LIMIT 1',
                (r[0],),
            )
            r2 = cur.fetchone()
            if r2 and r2[0]:
                return r2[0]
    if journal_name:
        cur.execute(
            'SELECT "JournalID" FROM "Journals" WHERE LOWER("JournalName") = LOWER(%s) LIMIT 1',
            (journal_name,),
        )
        r = cur.fetchone()
        if r:
            if issn_norm:
                cur.execute(
                    'UPDATE "Journals" SET "ISSN_Print" = COALESCE("ISSN_Print", %s) WHERE "JournalID" = %s',
                    (issn_norm, r[0]),
                )
            return r[0]
    return None


# ----------------------------------------------------------------------------
# Cooldown guard — same constant as backend/accounts/sync_views.py
# Defense-in-depth: refuses to call SerpAPI when invoked on a recently-
# synced researcher unless --force is passed. This protects against
# direct CLI invocations that bypass the API layer's gate.
# ----------------------------------------------------------------------------
SYNC_COOLDOWN_DAYS = 7


def check_cooldown(conn, user_id):
    """
    Returns (eligible, info_dict). When eligible is False, the caller
    should exit early without calling any external API.
    """
    cur = conn.cursor()
    cur.execute('''
        SELECT
            r."LastSyncedAt",
            (SELECT COUNT(*) FROM "Authors" a WHERE a."UserID" = %s) AS papers
        FROM "Users" u
        LEFT JOIN "Researcher" r ON r."UserID" = u."UserID"
        WHERE u."UserID" = %s
    ''', (user_id, user_id))
    row = cur.fetchone()
    cur.close()
    if not row:
        return False, {'reason': 'user_not_found'}
    last_synced, papers = row[0], row[1] or 0
    if last_synced is None:
        return True, {'reason': 'never_synced', 'papers': papers}
    from datetime import datetime, timedelta, timezone as tz
    now = datetime.now(tz.utc) if last_synced.tzinfo else datetime.utcnow()
    cooldown_until = last_synced + timedelta(days=SYNC_COOLDOWN_DAYS)
    if now < cooldown_until:
        return False, {
            'reason': 'in_cooldown',
            'last_synced_at': last_synced.isoformat(),
            'papers': papers,
            'cooldown_until': cooldown_until.isoformat(),
        }
    return True, {
        'reason': 'cooldown_elapsed',
        'last_synced_at': last_synced.isoformat(),
        'papers': papers,
    }


def main():
    if len(sys.argv) < 3:
        print("Usage: python scrapers/scholar.py <scholar_id> <user_id> [--force]")
        sys.exit(1)
    scholar_id = sys.argv[1]
    user_id = int(sys.argv[2])
    force = '--force' in sys.argv[3:]

    # Cooldown gate FIRST — before we burn a SerpAPI call.
    guard_conn = db()
    eligible, info = check_cooldown(guard_conn, user_id)
    guard_conn.close()
    if not eligible and not force:
        print(f"[SKIP] {info['reason']}: papers={info.get('papers', 0)}, "
              f"last_synced_at={info.get('last_synced_at')}, "
              f"cooldown_until={info.get('cooldown_until')}")
        print("[SKIP] Pass --force to override.")
        sys.exit(0)
    if force and info.get('reason') == 'in_cooldown':
        print(f"[FORCE] Overriding cooldown — papers already stored: "
              f"{info.get('papers', 0)}")

    print(f"=== Sync (Scholar={scholar_id}, UID={user_id}, "
          f"force={force}) ===\n")
    print("Fetching Scholar profile (3-pass)...")
    articles, cited_by_graph = fetch_scholar_profile(scholar_id)
    print(f"Got {len(articles)} unique articles, {len(cited_by_graph)} year-points\n")

    if not articles:
        print("No articles fetched")
        sys.exit(1)

    conn = db()
    cur = conn.cursor()
    cur.execute('SELECT "FullName_Ar" FROM "Users" WHERE "UserID" = %s', (user_id,))
    row = cur.fetchone()
    if not row:
        print(f"UserID {user_id} not found")
        sys.exit(1)
    print(f"Researcher: {row[0]}\n")

    n_inserted = n_existing = n_linked_new = 0
    n_enriched = n_journal_linked = 0

    for i, a in enumerate(articles, 1):
        title = (a.get("title") or "").strip()
        if not title:
            continue
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
            n_existing += 1
        else:
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
                n_inserted += 1
            except psycopg2.errors.UniqueViolation:
                cur.execute('ROLLBACK TO SAVEPOINT before_insert')
                cur.execute(
                    'SELECT "PaperID", "DOI", "JournalID" FROM "ResearchPaper" '
                    'WHERE LOWER("Title") = LOWER(%s) LIMIT 1',
                    (title,),
                )
                r = cur.fetchone()
                if not r:
                    continue
                paper_id, existing_doi, existing_journal = r
                n_existing += 1

        try:
            cb = a.get("cited_by") or {}
            scholar_cited = int(cb.get("value") or 0)
            cur.execute('''
                UPDATE "ResearchPaper"
                SET "RawData_Log" = jsonb_set(
                    COALESCE("RawData_Log", '{}'::jsonb),
                    '{cited_by_count}', to_jsonb(%s::int)
                )
                WHERE "PaperID" = %s
            ''', (scholar_cited, paper_id))
        except (ValueError, TypeError):
            pass

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
                n_enriched += 1
                if jid:
                    n_journal_linked += 1

        cur.execute('''
            INSERT INTO "Authors" (
                "UserID", "PaperID", "AuthorOrder",
                "IsCorrespondingAuthor", "MappingConfidence", "MappingCriteria",
                "AuthorNameRaw", "Is_Verified"
            )
            VALUES (%s, %s, NULL, FALSE, 1.0, 'scholar_id_verified', %s, TRUE)
            ON CONFLICT ("UserID", "PaperID") DO NOTHING
        ''', (user_id, paper_id, scholar_authors_str))
        if cur.rowcount > 0:
            n_linked_new += 1

        if i % 50 == 0:
            print(f"  progress: {i}/{len(articles)} (enriched={n_enriched}, journals={n_journal_linked})")
            conn.commit()

    cby_dict = {
        str(item.get("year")): int(item.get("citations") or 0)
        for item in cited_by_graph
        if item.get("year") is not None
    } or None

    cur.execute('''
        INSERT INTO "Researcher" ("UserID", "CitationsByYear", "LastSyncedAt")
        VALUES (%s, %s, NOW())
        ON CONFLICT ("UserID") DO UPDATE
        SET "CitationsByYear" = EXCLUDED."CitationsByYear",
            "LastSyncedAt"    = NOW()
    ''', (user_id, Json(cby_dict) if cby_dict else None))

    conn.commit()
    print(f"\nDone")
    print(f"  Articles:        {len(articles)}")
    print(f"  Inserted:        {n_inserted}")
    print(f"  Existing:        {n_existing}")
    print(f"  New links:       {n_linked_new}")
    print(f"  Enriched:        {n_enriched}")
    print(f"  Journal linked:  {n_journal_linked}")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
