"""Bind a Google Scholar ID to a UserID + trigger OpenAlex paper sync.

Why this script:
  - We keep finding Scholar profiles externally that the auto-scraper
    didn't link (faculty members who weren't in our seed CSV, profiles
    with non-standard email domains, etc.)
  - This is a focused 'just-do-it' helper: update Users.Scholar_ID,
    then call the main scraper's _sync_via_openalex_id flow for that
    single user.

Strategy:
  1. Update Users.Scholar_ID = <given>
  2. Search OpenAlex for the author by Scholar ID (using their internal
     index via 'ids.scholar' filter). If not found, fall back to ORCID
     lookup, or final fallback: name-based with institution validation.
  3. Sync all of that author's works (with counts_by_year).

Usage:
  python bind_scholar_id.py --user-id 42 --scholar-id 8Wd5_cwAAAAJ
  python bind_scholar_id.py --user-id 42 --scholar-id 8Wd5_cwAAAAJ --dry-run
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
SCHOLAR_BASE = "https://scholar.google.com/citations?user="


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


def find_openalex_by_orcid(orcid: str) -> Optional[dict]:
    """If Users has ORCID, OpenAlex lookup is deterministic."""
    try:
        return oa_get(f"/authors/orcid:{orcid}")
    except Exception:
        return None


def find_openalex_by_name_and_institution(name: str, institution_keyword: str = "al-baha") -> Optional[dict]:
    """Fallback: search by name, validate by institution."""
    try:
        data = oa_get("/authors", {"search": name, "per-page": 25})
    except Exception as e:
        print(f"    × name search failed: {e}")
        return None

    candidates = []
    for au in data.get("results", []):
        last_known = au.get("last_known_institutions", []) or []
        affiliations = au.get("affiliations", []) or []
        inst_names = []
        for inst in last_known:
            inst_names.append(normalize(inst.get("display_name", "")))
        for aff in affiliations:
            inst = aff.get("institution", {})
            inst_names.append(normalize(inst.get("display_name", "")))
        if any(institution_keyword in n for n in inst_names):
            candidates.append((au, inst_names))

    if not candidates:
        return None
    # Prefer smallest works_count (avoids merged profiles)
    candidates.sort(key=lambda x: x[0].get("works_count", 999))
    print(f"    ✓ name+inst match: {candidates[0][0].get('display_name')}  "
          f"works={candidates[0][0].get('works_count')}  insts={candidates[0][1][:2]}")
    return candidates[0][0]


def fetch_author_works(author_id: str) -> list[dict]:
    aid_short = author_id.replace("https://openalex.org/", "")
    works = []
    cursor = "*"
    while cursor:
        data = oa_get("/works", {
            "filter": f"author.id:{aid_short}",
            "per-page": 200,
            "cursor": cursor,
        })
        works.extend(data.get("results", []))
        cursor = data.get("meta", {}).get("next_cursor")
        time.sleep(0.1)
    return works


def upsert_paper(cur, work: dict, user_id: int, raw_name: str) -> tuple[int, str]:
    title = (work.get("title") or "").strip()
    if not title:
        return None, "skipped"
    doi = (work.get("doi") or "").replace("https://doi.org/", "").lower() or None
    pub_year = work.get("publication_year")
    cited = work.get("cited_by_count", 0)

    cby_list = work.get("counts_by_year") or []
    counts_by_year = {
        str(item["year"]): int(item.get("cited_by_count") or 0)
        for item in cby_list if item.get("year") is not None
    } or None

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
                    '{cited_by_count}', to_jsonb(%s::int)
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

    cur.execute("""
        INSERT INTO "Authors" (
            "UserID", "PaperID", "AuthorOrder",
            "IsCorrespondingAuthor", "MappingConfidence", "MappingCriteria",
            "AuthorNameRaw", "Is_Verified"
        )
        VALUES (%s, %s, NULL, FALSE, 0.95, 'scholar_id_bind', %s, TRUE)
        ON CONFLICT ("UserID", "PaperID") DO UPDATE
        SET "MappingConfidence" = EXCLUDED."MappingConfidence",
            "Is_Verified" = TRUE
    """, (user_id, paper_id, raw_name))
    return paper_id, action


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--scholar-id", required=True)
    parser.add_argument("--name", default="", help="Override raw name for Authors")
    parser.add_argument("--en-name", default="", help="English-transliterated name for OpenAlex search (e.g., 'Mahmoud Abu Ghali')")
    parser.add_argument("--max-works", type=int, default=50, help="Safety cap (merged-profile guard)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print("=" * 60)
    print(f"bind_scholar_id — UserID={args.user_id}  Scholar_ID={args.scholar_id}")
    print(f"Mode: {'DRY-RUN' if args.dry_run else 'WRITE'}")
    print("=" * 60)

    conn = db_connect()
    cur = conn.cursor()

    # 1. Verify user
    cur.execute("""
        SELECT "FullName_Ar", "FirstName", "LastName", "Email", "ORCID", "Scholar_ID"
        FROM "Users" WHERE "UserID" = %s
    """, (args.user_id,))
    row = cur.fetchone()
    if not row:
        print(f"[!] No user UserID={args.user_id}")
        sys.exit(1)
    full_ar, first, last, email, orcid, current_scholar = row
    print(f"  Researcher: {full_ar}  ({first} {last})  {email}")
    print(f"  Current Scholar_ID: {current_scholar or 'none'}  ORCID: {orcid or 'none'}")
    raw_name = args.name or full_ar or f"{first} {last}".strip()

    # 2. OpenAlex lookup
    print(f"\n[1] Resolving OpenAlex author...")
    oa_author = None
    if orcid:
        print(f"    trying ORCID {orcid}...")
        oa_author = find_openalex_by_orcid(orcid)
    if not oa_author and args.en_name:
        print(f"    trying English name search: {args.en_name!r}...")
        oa_author = find_openalex_by_name_and_institution(args.en_name)
    if not oa_author:
        print(f"    fallback: Arabic name+institution search ({raw_name!r})...")
        oa_author = find_openalex_by_name_and_institution(raw_name)
    if not oa_author:
        print(f"[!] Could not resolve OpenAlex author. Will only update Scholar_ID.")
        works = []
    else:
        works_count = oa_author.get("works_count", 0)
        if works_count > args.max_works:
            print(f"[!] Too many works ({works_count} > {args.max_works}) — likely merged profile")
            print(f"    Use a more specific filter or pass papers explicitly via sync_researcher_papers.py")
            sys.exit(1)
        print(f"    ✓ matched: {oa_author.get('display_name')}  "
              f"works={works_count}  ID={oa_author.get('id')}")
        print(f"\n[2] Fetching all works...")
        works = fetch_author_works(oa_author["id"])
        print(f"    fetched {len(works)} works")

    if args.dry_run:
        print("\n[DRY-RUN] Would:")
        print(f"  - Set Users.Scholar_ID = {args.scholar_id}")
        if oa_author:
            print(f"  - Set Researcher.OpenAlex_AuthorID = {oa_author.get('id')}")
            print(f"  - Upsert {len(works)} papers")
            for w in works[:10]:
                cby = w.get("counts_by_year") or []
                print(f"    • {w.get('publication_year')} — {(w.get('title') or '')[:65]}  ({len(cby)} buckets)")
            if len(works) > 10:
                print(f"    ... and {len(works) - 10} more")
        return

    try:
        # Update Users.Scholar_ID (preserve if already set)
        cur.execute("""
            UPDATE "Users" SET "Scholar_ID" = COALESCE("Scholar_ID", %s)
            WHERE "UserID" = %s
        """, (args.scholar_id, args.user_id))
        print(f"\n  ✓ Users.Scholar_ID set (rowcount={cur.rowcount})")

        if oa_author:
            oa_short = oa_author["id"].replace("https://openalex.org/", "")
            cur.execute("""
                UPDATE "Researcher"
                SET "OpenAlex_AuthorID" = COALESCE("OpenAlex_AuthorID", %s),
                    "ORCID_ID"          = COALESCE("ORCID_ID",          %s)
                WHERE "UserID" = %s
            """, (oa_short, oa_author.get("orcid"), args.user_id))
            if cur.rowcount == 0:
                cur.execute("""
                    INSERT INTO "Researcher" ("UserID", "OpenAlex_AuthorID", "ORCID_ID", "LastSyncedAt")
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT ("UserID") DO UPDATE
                    SET "OpenAlex_AuthorID" = COALESCE("Researcher"."OpenAlex_AuthorID", EXCLUDED."OpenAlex_AuthorID"),
                        "ORCID_ID"          = COALESCE("Researcher"."ORCID_ID",          EXCLUDED."ORCID_ID")
                """, (args.user_id, oa_short, oa_author.get("orcid")))
            print(f"  ✓ Researcher.OpenAlex_AuthorID = {oa_short}")

            n_ins = n_upd = 0
            for w in works:
                pid, action = upsert_paper(cur, w, args.user_id, raw_name)
                if pid:
                    if action == "inserted":
                        n_ins += 1
                    elif action == "updated":
                        n_upd += 1
            print(f"  ✓ papers: inserted={n_ins}, updated={n_upd}")

        conn.commit()
        print("\n✓ DONE — committed.")
    except Exception as e:
        conn.rollback()
        print(f"\n[!] Error — rolled back: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
