"""
Import via Paper Title — RG-to-OpenAlex Bridge
================================================
For researchers whose only public profile is ResearchGate (no API), this
script bridges the gap by using a SINGLE specific paper title from their
RG profile as a fingerprint to identify them in OpenAlex.

The architecture:

    RG Profile → user copies one specific paper title
              → OpenAlex /works search by title (deterministic)
              → response includes authorships with OpenAlex IDs + ORCIDs
              → fuzzy-match the researcher's display name in authorships
              → confirm institution match (Al-Baha University)
              → save canonical OpenAlex_AuthorID to the Researcher row
              → invoke _sync_via_openalex_id() to fetch ALL their works

WHY THIS BEATS NAME SEARCH:
    Saudi names like "Mohammed Alzahrani" are common; name+country search
    returns false positives. But ONE specific paper title is unique. Once
    we find that paper, the author whose ID is on it MUST be the right
    person — there's no ambiguity.

Usage:
    python import_via_paper_title.py \\
        --user-id 78 \\
        --name "Mohammed Alzahrani" \\
        --institution-keyword "Al Baha" \\
        --title "Venous thromboembolism in heart failure: incidence, phenotype-specific risk"

The title doesn't have to be exact — OpenAlex's title search handles
partial matches. Use a distinctive substring (10+ words is best).
"""

import os
import sys
import argparse
import re
import unicodedata
from typing import List, Dict, Optional, Tuple

import httpx
import psycopg2
from dotenv import load_dotenv

from litrix_scraper import (
    OPENALEX_BASE_URL,
    OPENALEX_HEADERS,
    OPENALEX_TIMEOUT,
    CONTACT_EMAIL,
    DB_CONFIG,
    extract_openalex_id,
    extract_orcid,
    _sync_via_openalex_id,
)

load_dotenv()


def normalize_for_match(text: str) -> str:
    """Normalize a string for fuzzy name comparison (lowercase, NFD, strip)."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = re.sub(r"[^\w\s]", " ", stripped.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def search_paper_by_title(title: str) -> Optional[Dict]:
    """
    Search OpenAlex for the most likely paper matching a title.
    Returns the top hit's full work JSON, or None.
    """
    if not title:
        return None
    try:
        r = httpx.get(
            f"{OPENALEX_BASE_URL}/works",
            headers=OPENALEX_HEADERS,
            params={
                "search":   title,
                "per-page": 5,
                "mailto":   CONTACT_EMAIL,
            },
            timeout=OPENALEX_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        results = (r.json() or {}).get("results", []) or []
        return results[0] if results else None
    except (httpx.HTTPError, ValueError) as e:
        print(f"[ERROR] OpenAlex title search failed: {e}")
        return None


def find_author_in_authorships(
    work: Dict,
    name: str,
    institution_keyword: Optional[str] = None,
) -> Optional[Tuple[str, Optional[str], str]]:
    """
    Locate the target researcher in a work's authorships.

    Returns (openalex_id, orcid, full_name) or None.

    Matching is layered:
        1. Normalized name token-set match (last name MUST match)
        2. If institution_keyword provided, the candidate's affiliation
           on this paper must contain the keyword (case-insensitive).
    """
    target_norm = normalize_for_match(name)
    target_tokens = set(target_norm.split())
    target_lastname = target_norm.split()[-1] if target_norm else ""

    candidates: List[Tuple[str, Optional[str], str]] = []

    for a in (work.get("authorships") or []):
        author = a.get("author") or {}
        display = (author.get("display_name") or "").strip()
        if not display:
            continue

        cand_norm = normalize_for_match(display)
        cand_tokens = set(cand_norm.split())

        cand_lastname = cand_norm.split()[-1] if cand_norm else ""
        if cand_lastname != target_lastname:
            continue
        overlap = target_tokens & cand_tokens
        if len(overlap) < 2:
            continue

        if institution_keyword:
            insts = a.get("institutions") or []
            inst_names = " ".join(
                (i.get("display_name") or "") for i in insts
            ).lower()
            if institution_keyword.lower() not in inst_names:
                continue

        oaid = extract_openalex_id(author.get("id"))
        orcid = extract_orcid(author.get("orcid"))
        if oaid:
            candidates.append((oaid, orcid, display))

    if not candidates:
        return None
    if len(candidates) > 1:
        print(f"[WARN] {len(candidates)} candidates matched on this paper:")
        for c in candidates:
            print(f"  {c[0]}  ORCID={c[1]}  display={c[2]}")
        print("[WARN] returning the first; verify before binding.")
    return candidates[0]


def get_user_info(user_id: int) -> Optional[Dict]:
    """Fetch the researcher row from the DB for a sanity check."""
    with psycopg2.connect(**DB_CONFIG) as conn:
        with conn.cursor() as cur:
            cur.execute('''
                SELECT u."FullName_Ar", r."OpenAlex_AuthorID",
                       r."ORCID_ID", r."ResearchGate_URL", r."LastSyncedAt"
                FROM "Users" u
                JOIN "Researcher" r ON r."UserID" = u."UserID"
                WHERE u."UserID" = %s
            ''', (user_id,))
            row = cur.fetchone()
    if not row:
        return None
    return {
        "full_name_ar":       row[0],
        "openalex_author_id": row[1],
        "orcid_id":           row[2],
        "researchgate_url":   row[3],
        "last_synced_at":     row[4],
    }


def bind_author_id(user_id: int, openalex_id: str,
                    orcid: Optional[str]) -> None:
    """Persist the resolved identifiers and clear any stale sync state."""
    with psycopg2.connect(**DB_CONFIG) as conn:
        with conn.cursor() as cur:
            cur.execute('''
                UPDATE "Researcher"
                SET "OpenAlex_AuthorID" = %s,
                    "ORCID_ID"          = COALESCE(%s, "ORCID_ID"),
                    "LastSyncedAt"      = NULL
                WHERE "UserID" = %s
            ''', (openalex_id, orcid, user_id))
        conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resolve an OpenAlex Author ID via a known paper title, "
                    "then sync the researcher's full work list."
    )
    parser.add_argument("--user-id", type=int, required=True,
                       help="The Litrix UserID of the researcher to bind")
    parser.add_argument("--name", required=True,
                       help="The English display name to find in authorships "
                            "(e.g., 'Mohammed Alzahrani')")
    parser.add_argument("--title", required=True,
                       help="A specific paper title (or substring) from the "
                            "researcher's RG/personal page. Distinct titles "
                            "yield reliable matches.")
    parser.add_argument("--institution-keyword", default=None,
                       help="Optional institution substring to confirm the "
                            "match (e.g., 'Al Baha'). Strongly recommended.")
    parser.add_argument("--dry-run", action="store_true",
                       help="Resolve and print the match but don't bind or sync.")
    args = parser.parse_args()

    print(f"\n[1] Looking up UserID={args.user_id} in DB...")
    info = get_user_info(args.user_id)
    if not info:
        print(f"  [FATAL] No researcher with UserID={args.user_id}")
        sys.exit(1)
    print(f"  → {info['full_name_ar']}")
    print(f"  → current OpenAlex_AuthorID = {info['openalex_author_id']}")
    print(f"  → current ORCID_ID          = {info['orcid_id']}")

    print(f"\n[2] Searching OpenAlex for paper title:")
    print(f"    \"{args.title}\"")
    work = search_paper_by_title(args.title)
    if not work:
        print("  [FATAL] No paper matched that title in OpenAlex.")
        sys.exit(2)
    print(f"  → matched: {work.get('title') or work.get('display_name')}")
    print(f"    DOI:     {work.get('doi')}")
    print(f"    year:    {work.get('publication_year')}")

    print(f"\n[3] Searching authorships for '{args.name}'"
          + (f" + institution containing '{args.institution_keyword}'"
             if args.institution_keyword else "") + "...")
    found = find_author_in_authorships(
        work, args.name, args.institution_keyword
    )
    if not found:
        print(f"  [FATAL] '{args.name}' not found in this paper's authorships")
        print("  Authors on this paper:")
        for a in (work.get("authorships") or [])[:10]:
            display = (a.get("author") or {}).get("display_name", "?")
            insts = ", ".join(
                (i.get("display_name") or "")
                for i in (a.get("institutions") or [])
            )
            print(f"    • {display}  [{insts}]")
        sys.exit(3)
    oaid, orcid, full_name = found
    print(f"  → MATCH:  {full_name}")
    print(f"    OpenAlex ID: {oaid}")
    print(f"    ORCID:       {orcid or '(none on this paper)'}")

    if args.dry_run:
        print("\n[DRY-RUN] Would bind these identifiers and start sync.")
        print("  To proceed, re-run without --dry-run.")
        return

    print(f"\n[4] Binding identifiers to UserID={args.user_id}...")
    bind_author_id(args.user_id, oaid, orcid)
    print("  ✓ saved")

    print(f"\n[5] Starting full sync via OpenAlex ID '{oaid}'...")
    stats = _sync_via_openalex_id(args.user_id, oaid, label=f"manual:{oaid}")
    print(f"\n[DONE] sync stats: {stats}")


if __name__ == "__main__":
    main()
