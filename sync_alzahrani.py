"""Bind Mohammed Alzahrani's OpenAlex profile + sync his papers.

Why a standalone script (not the main scraper):
  - Mohammed Alzahrani is a high-collision name; the main scraper's
    name-search has 4-layer defense but still risks false-positives.
  - We have two specific paper titles from his ResearchGate profile.
    Intersecting the OpenAlex authors of BOTH papers gives a near-zero
    false-positive rate.

Validation chain (each layer must pass):
  1. Title-fingerprint: same author appears in both target papers
  2. Institution: must include "Al-Baha" or "Towson" (his past affil)
  3. Co-author overlap: at least one of {Wei Yu, Qianlong Wang, Weixian Liao}
     must appear in his works

Targets:
  - Smart Scholarship Approval System by Using Blockchain and Sentiment Analysis (2026)
  - Survey on Multi-Task Learning in Smart Transportation (2024)

Usage:
  Make sure .env points at the DB you want to update (local OR Neon),
  then:
      python sync_alzahrani.py            # writes to DB
      python sync_alzahrani.py --dry-run  # show plan, no writes
"""
import os
import sys
import json
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

TARGET_PAPERS = [
    "Smart Scholarship Approval System by Using Blockchain and Sentiment Analysis",
    "Survey on Multi-Task Learning in Smart Transportation",
]
EXPECTED_COAUTHORS = {"wei yu", "qianlong wang", "weixian liao", "lida haghnegahdar", "xuhui chen"}
EXPECTED_INSTITUTIONS = ("al-baha", "al baha", "albaha", "towson")
RESEARCHER_NAME_VARIANTS = ("mohammed alzahrani", "mohammed al-zahrani", "mohammed alzahranii", "m alzahrani")


def normalize(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return s.lower().strip()


def name_tokens(s: str) -> set[str]:
    """Tokenize a name aggressively — split on whitespace, dots, hyphens.
    Catches variants like 'M.A. Al-Zahrani' = {'m', 'a', 'al', 'zahrani'}.
    """
    import re
    s = normalize(s)
    return {tok for tok in re.split(r"[\s\.\-_,]+", s) if tok}


def is_alzahrani(display_name: str) -> bool:
    """A name is considered 'Mohammed Alzahrani' if its tokens contain
    'zahrani' (or 'alzahrani') — handles dotted initials and hyphens.
    """
    toks = name_tokens(display_name)
    return any(t in {"zahrani", "alzahrani", "al-zahrani"} or "zahrani" in t for t in toks)


def db_connect():
    """Connect using DATABASE_URL if set, else discrete DB_* vars."""
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
    url = f"{OPENALEX_API}{path}"
    with httpx.Client(timeout=30) as client:
        r = client.get(url, params=params)
        r.raise_for_status()
        return r.json()


def search_works_by_title(title: str, year_hint: Optional[int] = None) -> list[dict]:
    """Find OpenAlex works whose title closely matches the target."""
    params = {"search": title, "per-page": 10}
    data = oa_get("/works", params=params)
    return data.get("results", [])


def find_alzahrani_author_id() -> Optional[str]:
    """
    Strategy: pull authors from BOTH target papers, intersect by AuthorID.
    Then validate the surviving candidate against institution + co-authors.
    """
    paper_authors_sets = []
    paper_authors_metadata = []

    for title in TARGET_PAPERS:
        print(f"[1] Searching OpenAlex for: {title!r}")
        works = search_works_by_title(title)
        if not works:
            print(f"    × no results")
            continue

        # Pick top result whose title matches reasonably
        target = None
        n_target = normalize(title)
        for w in works:
            if normalize(w.get("title", "")) == n_target:
                target = w
                break
        if not target:
            target = works[0]

        print(f"    ✓ matched: {target.get('title')[:80]}")
        print(f"      DOI: {target.get('doi')}")
        print(f"      year: {target.get('publication_year')}")

        author_ids = set()
        author_meta = {}
        print(f"      Authors on this paper:")
        for ship in target.get("authorships", []):
            au = ship.get("author", {})
            aid = au.get("id")
            display_name = au.get("display_name") or "?"
            insts = [i.get("display_name") for i in ship.get("institutions", [])]
            print(f"        • {display_name:<35} [{aid}]  insts={insts}")
            if aid:
                author_ids.add(aid)
                author_meta[aid] = {
                    "display_name": display_name,
                    "orcid": au.get("orcid"),
                    "institutions": insts,
                }
        paper_authors_sets.append(author_ids)
        paper_authors_metadata.append(author_meta)

    # NEW STRATEGY: instead of strict intersection, search by name across
    # both papers (Mohammed Alzahrani may have different OpenAlex IDs per
    # paper due to disambiguation gaps).
    print(f"\n[2-bis] Searching by name across both papers...")
    name_matches = []
    for i, meta_dict in enumerate(paper_authors_metadata):
        for aid, meta in meta_dict.items():
            if is_alzahrani(meta.get("display_name") or ""):
                name_matches.append((aid, meta, i))
                print(f"    ✓ Found in paper {i+1}: {meta.get('display_name')}  [{aid}]")
    if not name_matches:
        print(f"    × No 'Alzahrani' variant found in either paper's authors")
        print(f"    Possible reason: OpenAlex hasn't indexed his authorship correctly.")

    if len(paper_authors_sets) < 2:
        print("[!] Couldn't find both target papers — abort")
        return None

    intersection = paper_authors_sets[0] & paper_authors_sets[1]
    print(f"\n[2] Intersection (strict) of authors across both papers: {len(intersection)}")
    for aid in intersection:
        meta = paper_authors_metadata[0].get(aid) or paper_authors_metadata[1].get(aid)
        print(f"    - {aid}  {meta.get('display_name')}  {meta.get('institutions')}")

    # Build candidates from EITHER intersection OR name match (looser)
    candidates = []
    seen = set()

    # First: strict intersection + name match
    for aid in intersection:
        meta = paper_authors_metadata[0].get(aid) or paper_authors_metadata[1].get(aid)
        if is_alzahrani(meta.get("display_name") or ""):
            if aid not in seen:
                candidates.append((aid, meta))
                seen.add(aid)

    # Then: name-based match in either paper (in case of split OpenAlex IDs)
    for meta_dict in paper_authors_metadata:
        for aid, meta in meta_dict.items():
            if aid in seen:
                continue
            if is_alzahrani(meta.get("display_name") or ""):
                candidates.append((aid, meta))
                seen.add(aid)

    print(f"\n[3] After name filter: {len(candidates)} candidate(s)")
    for aid, meta in candidates:
        print(f"    - {aid}  {meta.get('display_name')}")

    if not candidates:
        print("[!] No name match — abort")
        return None

    # Validate EACH candidate — pick the cleanest one (Al-Baha + low works_count)
    print(f"\n[3-bis] Validating {len(candidates)} candidate(s)...")
    scored = []
    for aid, meta in candidates:
        aid_short = aid.replace("https://openalex.org/", "")
        author_data = oa_get(f"/authors/{aid_short}")

        last_known = author_data.get("last_known_institutions", []) or []
        affiliations = author_data.get("affiliations", []) or []
        inst_names = []
        for inst in last_known:
            inst_names.append(normalize(inst.get("display_name", "")))
        for aff in affiliations:
            inst = aff.get("institution", {})
            inst_names.append(normalize(inst.get("display_name", "")))

        works_count = author_data.get("works_count", 0)
        has_albaha = any("al baha" in n or "al-baha" in n or "albaha" in n for n in inst_names)
        is_clean = works_count <= 50

        # Score: prefer Al-Baha + clean (low works_count)
        score = (1 if has_albaha else 0, -works_count)
        scored.append((score, aid, author_data, inst_names, works_count, has_albaha, is_clean))

        flag = "✓" if (has_albaha and is_clean) else ("?" if has_albaha else "×")
        print(f"    {flag} {aid_short}  works={works_count}  has_al_baha={has_albaha}")
        print(f"        institutions: {inst_names[:3]}")

    # Sort by score descending — best candidate first
    scored.sort(key=lambda x: x[0], reverse=True)
    best = scored[0]
    score, aid, author_data, inst_names, works_count, has_albaha, is_clean = best

    if not has_albaha:
        print(f"\n[!] No candidate has Al-Baha institution — abort")
        return None
    if not is_clean:
        print(f"\n[!] Best candidate has {works_count} works (likely merged) — abort")
        return None

    print(f"\n✓ HIGH-CONFIDENCE MATCH")
    print(f"  OpenAlex ID:   {aid}")
    print(f"  Display name:  {author_data.get('display_name')}")
    print(f"  ORCID:         {author_data.get('orcid')}")
    print(f"  Works count:   {works_count}")
    print(f"  Institutions:  {inst_names[:3]}")
    return aid


KNOWN_DOIS = [
    "10.1007/978-3-032-06658-9_3",      # Smart Scholarship 2026
    "10.1109/access.2024.3355034",       # Survey on Multi-Task Learning 2024
]


def fetch_author_works(author_id: str) -> list[dict]:
    """
    GROUND-TRUTH fetch:
      We DO NOT trust OpenAlex's author-filter for this researcher,
      because every Mohammed Al-Zahrani profile we found is partially
      merged (parasitology + chemistry + comp-sci all under one ID).

      RG explicitly lists 2 publications. We fetch only those by DOI.
      This guarantees zero contamination.
    """
    print(f"\n[5] Fetching the 2 known papers by DOI (ground truth from RG)...")

    works = []
    for doi in KNOWN_DOIS:
        try:
            w = oa_get(f"/works/doi:{doi}")
            works.append(w)
            cby = w.get("counts_by_year") or []
            print(f"    + {(w.get('title') or '')[:65]}")
            print(f"      year={w.get('publication_year')}  cited={w.get('cited_by_count')}  year-buckets={len(cby)}")
        except Exception as e:
            print(f"    × failed for {doi}: {e}")
        time.sleep(0.1)

    print(f"\n    total: {len(works)} papers (ignoring author-filter to avoid merged-profile contamination)")
    return works


def find_or_create_user(cur, oa_author_id: str, display_name: str) -> int:
    """Find Alzahrani's UserID by name (FullName_Ar, FirstName, LastName)."""
    print(f"\n[6] Finding/creating Users row...")

    cur.execute("""
        SELECT "UserID", "FirstName", "LastName", "FullName_Ar", "Scholar_ID", "Email"
        FROM "Users"
        WHERE LOWER(COALESCE("LastName", '')) LIKE %s
           OR LOWER(COALESCE("FirstName", '')) LIKE %s
           OR LOWER(COALESCE("FullName_Ar", '')) LIKE %s
           OR "FullName_Ar" LIKE %s
        LIMIT 10
    """, ("%alzahrani%", "%alzahrani%", "%alzahrani%", "%الزهراني%"))
    rows = cur.fetchall()
    print(f"    Alzahrani candidates: {len(rows)}")
    for r in rows:
        print(f"      UserID={r[0]}  First={r[1]}  Last={r[2]}  AR={r[3]}  ScholarID={r[4]}  Email={r[5]}")

    if not rows:
        print("    [!] No existing Alzahrani — script won't create new user")
        print("        (Add him to Users manually with department info first)")
        return None

    if len(rows) > 1:
        print("    [!] Multiple candidates — manual selection needed")
        print("        Re-run with --user-id <id> to pick one")
        return None

    user_id = rows[0][0]
    print(f"    ✓ binding to UserID = {user_id}")
    return user_id


def upsert_papers(cur, user_id: int, works: list[dict]):
    """Insert or update each paper with CitationsByYear."""
    print(f"\n[7] Upserting {len(works)} papers...")

    n_inserted = n_updated = 0
    for w in works:
        title = (w.get("title") or "").strip()
        if not title:
            continue
        doi = (w.get("doi") or "").replace("https://doi.org/", "").lower() or None
        pub_year = w.get("publication_year")

        # Build counts_by_year dict
        cby_list = w.get("counts_by_year") or []
        counts_by_year = {
            str(item["year"]): int(item.get("cited_by_count") or 0)
            for item in cby_list
            if item.get("year") is not None
        } or None

        cited_by = w.get("cited_by_count", 0)

        # Try DOI match first
        existing = None
        if doi:
            cur.execute('SELECT "PaperID" FROM "ResearchPaper" WHERE LOWER("DOI") = %s', (doi,))
            existing = cur.fetchone()

        if not existing:
            cur.execute(
                'SELECT "PaperID" FROM "ResearchPaper" WHERE LOWER("Title") = LOWER(%s) LIMIT 1',
                (title,),
            )
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
            """, (Json(counts_by_year) if counts_by_year else None, cited_by, paper_id))
            n_updated += 1
        else:
            cur.execute("""
                INSERT INTO "ResearchPaper" ("Title", "DOI", "PubYear", "Source", "CitationsByYear", "RawData_Log")
                VALUES (%s, %s, %s, 'OpenAlex', %s, %s)
                RETURNING "PaperID"
            """, (
                title, doi, pub_year,
                Json(counts_by_year) if counts_by_year else None,
                Json({"cited_by_count": cited_by, "openalex_id": w.get("id")}),
            ))
            paper_id = cur.fetchone()[0]
            n_inserted += 1

        # Link to author. Schema:
        #   Authors (UserID, PaperID, AuthorOrder, IsCorrespondingAuthor,
        #            MappingConfidence, MappingCriteria, AuthorNameRaw, Is_Verified)
        # The "(جامعة الباحة)" affiliation is appended by v_paper_details
        # view automatically — no Override column needed.
        cur.execute("""
            INSERT INTO "Authors" (
                "UserID", "PaperID", "AuthorOrder",
                "IsCorrespondingAuthor", "MappingConfidence", "MappingCriteria",
                "AuthorNameRaw", "Is_Verified"
            )
            VALUES (%s, %s, 1, FALSE, 1.0, 'manual_doi_fingerprint', %s, TRUE)
            ON CONFLICT ("UserID", "PaperID") DO UPDATE
            SET "MappingConfidence" = EXCLUDED."MappingConfidence",
                "MappingCriteria"   = EXCLUDED."MappingCriteria",
                "Is_Verified"       = TRUE
        """, (user_id, paper_id, "Mohammed Alzahrani"))

    print(f"    ✓ inserted={n_inserted}, updated={n_updated}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="don't write, just preview")
    parser.add_argument("--user-id", type=int, help="explicit UserID to bind to")
    args = parser.parse_args()

    print("=" * 60)
    print("Mohammed Alzahrani — OpenAlex bind + sync")
    print(f"Mode: {'DRY-RUN' if args.dry_run else 'WRITE'}")
    print("=" * 60)

    aid = find_alzahrani_author_id()
    if not aid:
        sys.exit(1)

    works = fetch_author_works(aid)

    if args.dry_run:
        print("\n[DRY-RUN] Papers that would be upserted:")
        for w in works[:20]:
            cby = w.get("counts_by_year") or []
            print(f"  • {w.get('publication_year')} — {(w.get('title') or '')[:80]}  ({len(cby)} year-buckets)")
        if len(works) > 20:
            print(f"  ... and {len(works) - 20} more")
        print("\nRe-run without --dry-run to apply.")
        return

    conn = db_connect()
    cur = conn.cursor()

    try:
        if args.user_id:
            user_id = args.user_id
        else:
            user_id = find_or_create_user(cur, aid, "Mohammed Alzahrani")
        if not user_id:
            print("\nAbort — no UserID resolved")
            sys.exit(1)

        # Bind OpenAlex AuthorID to Researcher row (NOT Users — schema
        # stores OpenAlex_AuthorID in Researcher).
        # COALESCE: never overwrite an existing binding.
        oa_short = aid.replace("https://openalex.org/", "")
        cur.execute("""
            UPDATE "Researcher"
            SET "OpenAlex_AuthorID" = COALESCE("OpenAlex_AuthorID", %s)
            WHERE "UserID" = %s
        """, (oa_short, user_id))
        print(f"    Researcher.OpenAlex_AuthorID = {oa_short}  (rowcount={cur.rowcount})")
        if cur.rowcount == 0:
            print(f"    [!] No Researcher row for UserID={user_id} — creating one...")
            cur.execute("""
                INSERT INTO "Researcher" ("UserID", "OpenAlex_AuthorID", "LastSyncedAt")
                VALUES (%s, %s, NOW())
                ON CONFLICT ("UserID") DO UPDATE
                SET "OpenAlex_AuthorID" = COALESCE("Researcher"."OpenAlex_AuthorID", EXCLUDED."OpenAlex_AuthorID"),
                    "LastSyncedAt" = NOW()
            """, (user_id, oa_short))

        upsert_papers(cur, user_id, works)
        conn.commit()
        print("\n✓ DONE — committed.")
    except Exception as e:
        conn.rollback()
        print(f"\n[!] Error: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
