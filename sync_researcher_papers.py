"""Generic: bind a researcher's known papers to their UserID + sync metadata.

Why a generic script?
  - We keep finding papers OUTSIDE the auto-scraper's reach (manual RG
    profiles, EBSCO entries, Scholar email-searches). Each time we'd
    write a one-off script — clearly a `sync_X.py` per researcher
    doesn't scale.
  - This script accepts a UserID + any combination of DOIs/titles and
    handles the OpenAlex lookup, CitationsByYear extraction, paper
    upsert, and Authors linking for any researcher.

What it does NOT do (intentional separation of concerns):
  - It does not bind OpenAlex_AuthorID. For high-collision names
    (Mohammed Alzahrani, Hassan, Ahmed) the AuthorID is unreliable.
    Use sync_alzahrani.py-style fingerprinting only when you have ≥2
    target papers AND a clear institutional fingerprint.
  - It does not create new Users rows. The user must already exist.

Usage:
  Sync 2 papers by DOI:
      python sync_researcher_papers.py --user-id 42 --doi 10.1109/access.2024.xxx --doi 10.1007/...

  Sync 1 paper by title (no DOI):
      python sync_researcher_papers.py --user-id 42 --title "AI-Driven Financial Crime ..."

  Mixed + dry-run:
      python sync_researcher_papers.py --user-id 42 --doi 10.x/y --title "..." --dry-run
"""
import os
import sys
import time
import argparse
import unicodedata
from typing import Optional

import httpx
import psycopg2
from psycopg2.extras import Json
from dotenv import load_dotenv

load_dotenv()

OPENALEX_API = "https://api.openalex.org"
EMAIL = os.getenv("OPENALEX_EMAIL", "ra20awn@gmail.com")


def normalize(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return s.lower().strip()


def db_connect():
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


def oa_get(path: str, params: dict | None = None) -> dict:
    params = dict(params or {})
    params["mailto"] = EMAIL
    with httpx.Client(timeout=30) as client:
        r = client.get(f"{OPENALEX_API}{path}", params=params)
        r.raise_for_status()
        return r.json()


def fetch_by_doi(doi: str) -> Optional[dict]:
    doi_clean = doi.replace("https://doi.org/", "").replace("http://doi.org/", "").lower()
    try:
        return oa_get(f"/works/doi:{doi_clean}")
    except Exception as e:
        print(f"    × DOI fetch failed for {doi}: {e}")
        return None


def fetch_by_title(title: str) -> Optional[dict]:
    """Search by title; pick best title match."""
    n_target = normalize(title)
    try:
        data = oa_get("/works", {"search": title, "per-page": 5})
    except Exception as e:
        print(f"    × title search failed: {e}")
        return None
    results = data.get("results", []) or []
    if not results:
        return None
    # Prefer exact normalized match
    for w in results:
        if normalize(w.get("title", "")) == n_target:
            return w
    # Otherwise return top result if title overlap is high
    top = results[0]
    n_top = normalize(top.get("title", ""))
    if n_target[:30] in n_top or n_top[:30] in n_target:
        return top
    print(f"    ? best result didn't match closely:")
    print(f"      wanted: {title[:80]}")
    print(f"      got:    {top.get('title', '')[:80]}")
    return None


def upsert_paper(cur, work: dict, user_id: int, raw_name: str) -> tuple[int, str]:
    """Insert or update ResearchPaper + link to UserID via Authors.
    Returns (paper_id, action)."""
    title = (work.get("title") or "").strip()
    doi = (work.get("doi") or "").replace("https://doi.org/", "").lower() or None
    pub_year = work.get("publication_year")
    cited = work.get("cited_by_count", 0)

    cby_list = work.get("counts_by_year") or []
    counts_by_year = {
        str(item["year"]): int(item.get("cited_by_count") or 0)
        for item in cby_list
        if item.get("year") is not None
    } or None

    # Find existing
    existing = None
    if doi:
        cur.execute('SELECT "PaperID" FROM "ResearchPaper" WHERE LOWER("DOI") = %s', (doi,))
        existing = cur.fetchone()
    if not existing:
        cur.execute('SELECT "PaperID" FROM "ResearchPaper" WHERE LOWER("Title") = LOWER(%s) LIMIT 1', (title,))
        existing = cur.fetchone()

    if existing:
        paper_id = existing[0]
        cur.execute("""
            UPDATE "ResearchPaper"
            SET "CitationsByYear" = COALESCE(%s, "CitationsByYear"),
                "RawData_Log" = jsonb_set(
                    COALESCE("RawData_Log", '{}'::jsonb),
                    '{cited_by_count}',
                    to_jsonb(%s::int)
                )
            WHERE "PaperID" = %s
        """, (Json(counts_by_year) if counts_by_year else None, cited, paper_id))
        action = "updated"
    else:
        cur.execute("""
            INSERT INTO "ResearchPaper" ("Title", "DOI", "PubYear", "Source", "CitationsByYear", "RawData_Log")
            VALUES (%s, %s, %s, 'OpenAlex', %s, %s)
            RETURNING "PaperID"
        """, (
            title, doi, pub_year,
            Json(counts_by_year) if counts_by_year else None,
            Json({"cited_by_count": cited, "openalex_id": work.get("id")}),
        ))
        paper_id = cur.fetchone()[0]
        action = "inserted"

    # Link to user via Authors. Affiliation override is handled by view.
    cur.execute("""
        INSERT INTO "Authors" (
            "UserID", "PaperID", "AuthorOrder",
            "IsCorrespondingAuthor", "MappingConfidence", "MappingCriteria",
            "AuthorNameRaw", "Is_Verified"
        )
        VALUES (%s, %s, 1, FALSE, 1.0, 'manual_doi_or_title', %s, TRUE)
        ON CONFLICT ("UserID", "PaperID") DO UPDATE
        SET "MappingConfidence" = EXCLUDED."MappingConfidence",
            "MappingCriteria"   = EXCLUDED."MappingCriteria",
            "Is_Verified"       = TRUE
    """, (user_id, paper_id, raw_name))

    return paper_id, action


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--doi", action="append", default=[], help="DOI (can repeat)")
    parser.add_argument("--title", action="append", default=[], help="Title (can repeat)")
    parser.add_argument("--name", default="", help="Optional raw name for Authors.AuthorNameRaw")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.doi and not args.title:
        print("[!] Provide at least one --doi or --title")
        sys.exit(1)

    print("=" * 60)
    print(f"sync_researcher_papers — UserID={args.user_id}")
    print(f"Mode: {'DRY-RUN' if args.dry_run else 'WRITE'}")
    print("=" * 60)

    # Resolve user info
    conn = db_connect()
    cur = conn.cursor()
    cur.execute('SELECT "FullName_Ar", "FirstName", "LastName", "Email" FROM "Users" WHERE "UserID" = %s',
                (args.user_id,))
    row = cur.fetchone()
    if not row:
        print(f"[!] No user with UserID = {args.user_id}")
        sys.exit(1)
    full_ar, first, last, email = row
    print(f"  Researcher: {full_ar}  ({first} {last})  {email}")
    raw_name = args.name or full_ar or f"{first} {last}".strip()

    # Fetch all works
    works = []
    print(f"\n[1] Fetching {len(args.doi)} DOI(s) + {len(args.title)} title(s) from OpenAlex...")
    for doi in args.doi:
        w = fetch_by_doi(doi)
        if w:
            works.append(w)
            cby = w.get("counts_by_year") or []
            print(f"    + DOI {doi}: {(w.get('title') or '')[:60]}  ({len(cby)} year-buckets)")
        time.sleep(0.1)
    for title in args.title:
        w = fetch_by_title(title)
        if w:
            works.append(w)
            cby = w.get("counts_by_year") or []
            print(f"    + title: {(w.get('title') or '')[:60]}  ({len(cby)} year-buckets)")
        time.sleep(0.1)

    # De-duplicate by OpenAlex work ID
    seen = set()
    unique = []
    for w in works:
        wid = w.get("id")
        if wid and wid not in seen:
            unique.append(w)
            seen.add(wid)
    works = unique
    print(f"\n  Total unique works: {len(works)}")

    if not works:
        print("[!] No works resolved — abort")
        sys.exit(1)

    if args.dry_run:
        print("\n[DRY-RUN] Papers that would be upserted:")
        for w in works:
            cby = w.get("counts_by_year") or []
            print(f"  • {w.get('publication_year')} — cited={w.get('cited_by_count')}  buckets={len(cby)}  {(w.get('title') or '')[:70]}")
        return

    print(f"\n[2] Upserting...")
    n_ins = n_upd = 0
    try:
        for w in works:
            pid, action = upsert_paper(cur, w, args.user_id, raw_name)
            print(f"    {action:>8}  PaperID={pid}  {(w.get('title') or '')[:60]}")
            if action == "inserted":
                n_ins += 1
            else:
                n_upd += 1
        conn.commit()
        print(f"\n✓ DONE — inserted={n_ins}, updated={n_upd}, committed.")
    except Exception as e:
        conn.rollback()
        print(f"\n[!] Error — rolled back: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
