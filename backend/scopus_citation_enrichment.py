"""
Citation Enrichment for Scopus-imported papers.

=============================================================================
WHY THIS EXISTS
=============================================================================
scopus_attribution_fix.py imports paper metadata (Title, DOI, Abstract, etc.)
from the Scopus Excel exports — but Scopus exports DON'T include per-year
citation breakdowns. The Litrix dashboard reads `ResearchPaper.CitationsByYear`
to render the citations chart, so without it papers show "0 citations" even
when they have real citations elsewhere.

This script enriches those papers by fetching counts_by_year from OpenAlex
(which indexes Scopus and provides per-year data via DOI lookup).

=============================================================================
ARCHITECTURE
=============================================================================
1. SELECT papers where Source='Scopus' AND DOI IS NOT NULL AND CitationsByYear
   is empty/missing. (Optionally filter by --user-id to scope to one researcher.)
2. For each paper: call OpenAlex `/works/doi:{DOI}` endpoint.
3. Convert OpenAlex `counts_by_year` (list of {year, cited_by_count}) to the
   JSONB shape Litrix expects: `{"YYYY": N, ...}`.
4. UPDATE ResearchPaper.CitationsByYear.

OpenAlex is free + unauthenticated for polite use. We send a User-Agent
with our contact email per their etiquette guide (raises rate limit pool).

=============================================================================
USAGE
=============================================================================
    # Dry run — count what would update, show samples (SAFE)
    python backend/scopus_citation_enrichment.py --dry-run

    # Apply to ALL Scopus papers without citations
    python backend/scopus_citation_enrichment.py --apply

    # Single researcher only (recommended for first test)
    python backend/scopus_citation_enrichment.py --user-id 106 --apply

    # Limit batch size for testing
    python backend/scopus_citation_enrichment.py --apply --limit 5
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg2
import httpx
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
except Exception:
    pass

warnings.filterwarnings("ignore")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


# =============================================================================
# CONFIGURATION
# =============================================================================

OPENALEX_BASE = "https://api.openalex.org"

# Polite User-Agent — OpenAlex grants higher rate limits to identified clients.
# Picks the CONTACT_EMAIL from .env (matches the convention in scrapers/*.py).
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "noreply@example.com")
USER_AGENT = f"LitrixCitationEnrichment/1.0 (mailto:{CONTACT_EMAIL})"

# Delay between API calls. OpenAlex allows ~10 req/sec for polite clients;
# we go a bit slower (5/sec) to be courteous + leave headroom for retries.
RATE_LIMIT_DELAY_SEC = 0.2

# Where to write the audit log
AUDIT_LOG_DIR = PROJECT_ROOT / "data" / "scopus_audit"

# UserIDs touched by scopus_attribution_fix — for --user-id validation
EXPECTED_USER_IDS = [6, 8, 81, 89, 92, 93, 106]


# =============================================================================
# DATABASE
# =============================================================================

# Shared DB helper (single source — litrix_db.py lives in backend/).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from litrix_db import db as db_connect


def select_papers_to_enrich(conn, user_id: int | None, limit: int | None) -> list[dict]:
    """Find ResearchPaper rows that need citation enrichment.

    Targets papers from ANY source (Scholar, OpenAlex, ORCID, Scopus, etc.):
        • DOI IS NOT NULL (we can look them up on OpenAlex)
        • CitationsByYear IS NULL OR is the empty JSON object {}

    The previous version restricted to Source='Scopus' — that was an
    oversight that left Scholar/ORCID-imported papers with stale or
    missing citation data. Citations are source-agnostic: any paper
    with a DOI deserves an enriched timeline.
    """
    where_clauses = [
        "rp.\"DOI\" IS NOT NULL",
        "rp.\"DOI\" <> ''",
        "(rp.\"CitationsByYear\" IS NULL OR rp.\"CitationsByYear\"::text = '{}')",
    ]
    params: list[Any] = []

    base_sql = '''
        SELECT DISTINCT rp."PaperID", rp."DOI", rp."Title", rp."PubYear"
        FROM "ResearchPaper" rp
    '''

    if user_id is not None:
        base_sql += ' JOIN "Authors" a ON a."PaperID" = rp."PaperID" '
        where_clauses.append('a."UserID" = %s')
        params.append(user_id)

    sql = base_sql + " WHERE " + " AND ".join(where_clauses) + " ORDER BY rp.\"PaperID\""
    if limit:
        sql += f" LIMIT {int(limit)}"

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def update_paper_citations(conn, paper_id: int, citations_by_year: dict[str, int]) -> None:
    """Write the per-year citations dict into the paper row.

    ALSO patches RawData_Log to add `cited_by_count` (total). The Litrix
    dashboard's per-paper aggregation query (analytics/views.py) reads:
        COALESCE(
            ("RawData_Log"->'cited_by'->>'value')::int,    -- Scholar format
            ("RawData_Log"->>'cited_by_count')::int,        -- OpenAlex format
            0)
    Without `cited_by_count`, Scopus papers display "0 CITATIONS" on the
    right side even though the per-year chips render correctly.
    """
    total = sum(int(v) for v in citations_by_year.values())
    with conn.cursor() as cur:
        cur.execute(
            '''
            UPDATE "ResearchPaper"
            SET "CitationsByYear" = %s::jsonb,
                "RawData_Log" = jsonb_set(
                    COALESCE("RawData_Log", '{}'::jsonb),
                    '{cited_by_count}',
                    to_jsonb(%s::int)
                )
            WHERE "PaperID" = %s
            ''',
            (json.dumps(citations_by_year), total, paper_id),
        )


# =============================================================================
# OPENALEX
# =============================================================================

def fetch_openalex_by_doi(client: httpx.Client, doi: str) -> dict | None:
    """Hit /works/doi:{DOI}. Returns the work JSON or None if not indexed.

    OpenAlex accepts DOIs with or without the "https://doi.org/" prefix.
    It returns 404 for unknown DOIs — we treat that as "not in OpenAlex"
    and skip gracefully.
    """
    # Normalize: strip any URL prefix, lowercase (OpenAlex is case-insensitive)
    clean_doi = doi.strip().lower().removeprefix("https://doi.org/").removeprefix("doi.org/")
    url = f"{OPENALEX_BASE}/works/doi:{clean_doi}"
    try:
        resp = client.get(url, headers={"User-Agent": USER_AGENT}, timeout=10.0)
    except httpx.HTTPError as e:
        print(f"     [http error] {type(e).__name__}: {e}")
        return None

    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        print(f"     [unexpected status {resp.status_code}] {resp.text[:120]}")
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def extract_counts_by_year(work: dict) -> dict[str, int]:
    """OpenAlex returns counts_by_year as [{"year": Y, "cited_by_count": N}, ...].

    We flatten to {"Y": N} and drop zero/missing entries — the Litrix dashboard
    treats absence as zero.
    """
    raw = work.get("counts_by_year") or []
    result: dict[str, int] = {}
    for entry in raw:
        year = entry.get("year")
        count = entry.get("cited_by_count")
        if year is None or count is None or count == 0:
            continue
        result[str(year)] = int(count)
    return result


# =============================================================================
# CLI / ORCHESTRATION
# =============================================================================

def save_audit(records: list[dict]) -> Path:
    AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = AUDIT_LOG_DIR / f"citation_enrichment_{timestamp}.json"
    out_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return out_path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be enriched (no DB writes, no API calls).")
    ap.add_argument("--apply", action="store_true",
                    help="Fetch from OpenAlex and update CitationsByYear.")
    ap.add_argument("--user-id", type=int, default=None,
                    help="Scope to one researcher only (recommended for first test).")
    ap.add_argument("--limit", type=int, default=None,
                    help="Process at most N papers (helpful for trial runs).")
    args = ap.parse_args()

    if not args.dry_run and not args.apply:
        ap.error("Specify either --dry-run or --apply.")

    if args.user_id is not None and args.user_id not in EXPECTED_USER_IDS:
        print(f"WARNING: UserID {args.user_id} is not one of the 7 researchers "
              f"touched by scopus_attribution_fix. Proceeding anyway.")

    print(f"Connecting to database...")
    try:
        conn = db_connect()
    except Exception as e:
        print(f"ERROR connecting to database: {e}")
        return 1

    try:
        papers = select_papers_to_enrich(conn, args.user_id, args.limit)
        print(f"\nFound {len(papers)} papers needing citation enrichment.")

        if not papers:
            print("Nothing to do.")
            return 0

        if args.dry_run:
            print("\n=== Sample (first 5) ===")
            for p in papers[:5]:
                print(f"  PaperID {p['PaperID']:>5}  [{p['PubYear']}]  "
                      f"DOI: {p['DOI']}")
                print(f"    Title: {(p['Title'] or '')[:90]}")
            print(f"\n[Dry-run only — no API calls made, no DB writes.]")
            print(f"Re-run with --apply to actually fetch and update.")
            return 0

        # APPLY MODE
        print(f"\nUsing OpenAlex contact email: {CONTACT_EMAIL}")
        print(f"Rate limit: {1/RATE_LIMIT_DELAY_SEC:.0f} req/sec (estimated "
              f"{len(papers) * RATE_LIMIT_DELAY_SEC:.0f}s total).\n")

        audit: list[dict] = []
        stats = {"enriched": 0, "not_in_openalex": 0, "no_citations": 0, "errors": 0}

        with httpx.Client() as client:
            for i, p in enumerate(papers, 1):
                paper_id = p["PaperID"]
                doi = p["DOI"]
                title_preview = (p["Title"] or "")[:60]
                print(f"  [{i:>4}/{len(papers)}] PaperID {paper_id:>5}  {title_preview}...")

                work = fetch_openalex_by_doi(client, doi)
                if work is None:
                    print(f"          -> not in OpenAlex (skip)")
                    stats["not_in_openalex"] += 1
                    audit.append({"paper_id": paper_id, "doi": doi,
                                  "action": "skipped_not_found"})
                    time.sleep(RATE_LIMIT_DELAY_SEC)
                    continue

                counts = extract_counts_by_year(work)
                if not counts:
                    print(f"          -> no citations recorded")
                    stats["no_citations"] += 1
                    audit.append({"paper_id": paper_id, "doi": doi,
                                  "action": "no_citations",
                                  "openalex_id": work.get("id")})
                    time.sleep(RATE_LIMIT_DELAY_SEC)
                    continue

                # Commit per-paper so a mid-run interruption keeps progress.
                try:
                    update_paper_citations(conn, paper_id, counts)
                    conn.commit()
                    total = sum(counts.values())
                    print(f"          -> {total} citations across "
                          f"{len(counts)} years  {dict(sorted(counts.items()))}")
                    stats["enriched"] += 1
                    audit.append({"paper_id": paper_id, "doi": doi,
                                  "action": "enriched",
                                  "total_citations": total,
                                  "counts_by_year": counts})
                except Exception as e:
                    conn.rollback()
                    print(f"          -> DB error: {e}")
                    stats["errors"] += 1
                    audit.append({"paper_id": paper_id, "doi": doi,
                                  "action": "db_error", "error": str(e)})

                time.sleep(RATE_LIMIT_DELAY_SEC)

        print("\n" + "=" * 80)
        print(f" RESULTS ".center(80, "="))
        print("=" * 80)
        print(f"  Enriched (citations added):  {stats['enriched']:>4}")
        print(f"  No citations on OpenAlex:    {stats['no_citations']:>4}")
        print(f"  Not in OpenAlex index:       {stats['not_in_openalex']:>4}")
        print(f"  DB errors:                   {stats['errors']:>4}")
        print(f"  Total processed:             {len(papers):>4}")

        out_path = save_audit(audit)
        print(f"\nAudit log saved to:\n  {out_path}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
