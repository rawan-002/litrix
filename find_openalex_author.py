"""
Litrix Diagnostic Tool — Manual OpenAlex Author Resolution
============================================================
For researchers whose name is too common for automated matching, this
script narrows candidates by combining:

    1. Institution filter (e.g., 'Al Baha University') — uses OpenAlex's
       institution graph, much stronger than country alone.
    2. Name fuzzy match — display_name.search.
    3. Manual review — prints all candidates with works count, paper
       samples, and affiliation history so an admin can pick correctly.

Usage:
    python find_openalex_author.py "Mohammed Alzahrani"
    python find_openalex_author.py "Mohammed Alzahrani" --institution "Al Baha"
    python find_openalex_author.py "Mohammed Alzahrani" --institution "Al Baha" --max-works 100

Output: a numbered list of candidates. The admin notes the correct
OpenAlex Author ID and the User ID, then runs the SQL the script prints
at the end to bind them.
"""

import os
import sys
import argparse
from typing import List, Dict, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

OPENALEX_BASE_URL = "https://api.openalex.org"
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "ra20awn@gmail.com")
HEADERS = {"User-Agent": f"Litrix/1.0 (mailto:{CONTACT_EMAIL})"}


def find_institution(query: str) -> Optional[Dict]:
    """Find the most likely OpenAlex institution matching a search term."""
    r = httpx.get(
        f"{OPENALEX_BASE_URL}/institutions",
        headers=HEADERS,
        params={"search": query, "per-page": 5, "mailto": CONTACT_EMAIL},
        timeout=30.0,
    )
    if r.status_code != 200:
        return None
    results = (r.json() or {}).get("results", [])
    return results[0] if results else None


def search_authors(name: str, institution_id: Optional[str] = None,
                   max_results: int = 10) -> List[Dict]:
    """Search OpenAlex for authors by name, optionally institution-scoped."""
    params = {
        "search":   name,
        "per-page": max_results,
        "mailto":   CONTACT_EMAIL,
    }
    if institution_id:
        params["filter"] = f"last_known_institutions.id:{institution_id}"

    r = httpx.get(
        f"{OPENALEX_BASE_URL}/authors",
        headers=HEADERS,
        params=params,
        timeout=30.0,
    )
    if r.status_code != 200:
        return []
    return (r.json() or {}).get("results", []) or []


def fetch_recent_titles(author_id: str, n: int = 3) -> List[str]:
    """Pull the N most-recent paper titles for a candidate author —
    helps the admin verify identity by topic match against ResearchGate."""
    r = httpx.get(
        f"{OPENALEX_BASE_URL}/works",
        headers=HEADERS,
        params={
            "filter":   f"author.id:{author_id}",
            "per-page": n,
            "sort":     "publication_year:desc",
            "mailto":   CONTACT_EMAIL,
        },
        timeout=30.0,
    )
    if r.status_code != 200:
        return []
    return [
        (w.get("title") or w.get("display_name") or "")[:100]
        for w in (r.json() or {}).get("results", []) or []
    ]


def short_oaid(full_url: Optional[str]) -> str:
    if not full_url:
        return "?"
    return full_url.rstrip("/").rsplit("/", 1)[-1]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manually resolve an OpenAlex Author ID when automated "
                    "name search returns false positives."
    )
    parser.add_argument("name", help="Author display name (e.g., 'Mohammed Alzahrani')")
    parser.add_argument(
        "--institution", default=None,
        help="Institution search term (e.g., 'Al Baha University')"
    )
    parser.add_argument(
        "--max-works", type=int, default=None,
        help="Filter out candidates with works_count exceeding this threshold."
    )
    parser.add_argument(
        "--limit", type=int, default=10,
        help="Max number of candidates to list (default 10)"
    )
    args = parser.parse_args()

    print(f"\nSearching OpenAlex for: {args.name}")

    inst_id_short: Optional[str] = None
    if args.institution:
        print(f"  scoped to institution: {args.institution}")
        inst = find_institution(args.institution)
        if not inst:
            print(f"  [WARN] couldn't find institution '{args.institution}' — "
                  f"continuing without institution filter")
        else:
            inst_id_short = short_oaid(inst.get("id"))
            print(f"  → matched institution: {inst.get('display_name')} "
                  f"({inst_id_short})  country={inst.get('country_code')}")

    candidates = search_authors(args.name, inst_id_short, max_results=args.limit)
    if not candidates:
        print("\nNo candidates returned. Try a less restrictive search.")
        return

    if args.max_works is not None:
        kept = [c for c in candidates if (c.get("works_count") or 0) <= args.max_works]
        print(f"\nFiltered to works_count ≤ {args.max_works}: "
              f"{len(kept)} of {len(candidates)} candidates")
        candidates = kept

    if not candidates:
        print("\nAll candidates filtered out. Try raising --max-works.")
        return

    print("\n" + "=" * 70)
    print("CANDIDATE LIST")
    print("=" * 70)
    for i, c in enumerate(candidates, 1):
        oaid = short_oaid(c.get("id"))
        name = c.get("display_name", "?")
        works = c.get("works_count", 0)
        cited = c.get("cited_by_count", 0)
        orcid = c.get("orcid")
        affil_list = c.get("last_known_institutions") or []
        affil = (affil_list[0].get("display_name") if affil_list else "?")
        country = (affil_list[0].get("country_code") if affil_list else "?")

        print(f"\n[{i}] {oaid}  ←  {name}")
        print(f"    institution : {affil} ({country})")
        print(f"    works_count : {works}")
        print(f"    cited_by    : {cited}")
        if orcid:
            print(f"    ORCID       : {orcid}")

        titles = fetch_recent_titles(oaid, n=3)
        if titles:
            print(f"    recent      :")
            for t in titles:
                print(f"      • {t}")

    print("\n" + "=" * 70)
    print("HOW TO USE THIS RESULT")
    print("=" * 70)
    print(
        "Compare the 'recent' paper titles against the researcher's actual\n"
        "ResearchGate profile. The candidate whose recent papers MATCH the\n"
        "RG profile's papers is the correct one.\n\n"
        "Once you identify the correct OpenAlex Author ID, save it to the\n"
        "Researcher row in the DB:\n"
    )
    print("    UPDATE \"Researcher\"")
    print("    SET \"OpenAlex_AuthorID\" = 'A_correct_id_here',")
    print("        \"LastSyncedAt\" = NULL")
    print("    WHERE \"UserID\" = (")
    print("        SELECT u.\"UserID\" FROM \"Users\" u")
    print("        WHERE u.\"FullName_Ar\" LIKE '%محمد عبدالله خضران%'")
    print("    );\n")
    print("Then sync via:")
    print("    python -c \"from litrix_scraper import _sync_via_openalex_id; "
          "_sync_via_openalex_id(USER_ID, 'A_correct_id', 'manual')\"\n")


if __name__ == "__main__":
    main()
