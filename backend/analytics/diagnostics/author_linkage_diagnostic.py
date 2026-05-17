"""
Litrix — Author Linkage Diagnostic Script
==========================================
Scans the database for "orphaned" research papers (papers with no internal
Authors link) and surfaces ExternalAuthors records that likely belong to a
registered Researcher.

WHY THIS SCRIPT EXISTS
----------------------
Some papers exist in `ResearchPaper` but have no row in `Authors` connecting
them to a registered `Researcher.UserID`. This breaks the core promise of
Litrix: "every paper must trace back to its author."

This script does NOT mutate data. It produces a CSV report so the team can
review the scope of the linkage gap before running any reconciliation job.

USAGE
-----
    python author_linkage_diagnostic.py --output ./reports/

Run from Django shell context (uses Django ORM):
    python manage.py shell < author_linkage_diagnostic.py
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Iterable, List

# NOTE: Adjust imports to your actual app structure.
# from analytics.models import ResearchPaper, Authors, ExternalAuthors, User, Researcher
# from django.db.models import Q, Count

try:
    from django.db import connection
except ImportError:
    connection = None  # Allows static analysis even outside Django.


# ---------------------------------------------------------------------------
# Data containers (Type-safe, predictable output)
# ---------------------------------------------------------------------------

@dataclass
class OrphanedPaper:
    paper_id: int
    title: str
    source: str
    scraped_at: str
    external_authors_count: int


@dataclass
class PossibleMatch:
    ext_author_id: int
    paper_id: int
    scraped_name: str
    possible_researcher_id: int
    registered_name: str
    match_score: float


# ---------------------------------------------------------------------------
# Diagnostic Queries
# ---------------------------------------------------------------------------

ORPHANED_PAPERS_SQL = """
SELECT
    rp."PaperID",
    rp."Title",
    COALESCE(rp."Source", 'unknown')        AS "Source",
    rp."ScrapedAt",
    COUNT(ea."ExtAuthorID")                  AS "ExternalAuthorsCount"
FROM "ResearchPaper" rp
LEFT JOIN "Authors"         a  ON rp."PaperID" = a."PaperID"
LEFT JOIN "ExternalAuthors" ea ON rp."PaperID" = ea."PaperID"
WHERE a."UserID" IS NULL
GROUP BY rp."PaperID", rp."Title", rp."Source", rp."ScrapedAt"
ORDER BY rp."ScrapedAt" DESC NULLS LAST;
"""


# Requires pg_trgm extension. Run once on the DB:
#   CREATE EXTENSION IF NOT EXISTS pg_trgm;
# NOTE: The actual implementation uses table name "Users" (plural).
# The schema PDF lists it as "User" — adjusted here to match the live DB.
POSSIBLE_MATCHES_SQL = """
SELECT
    ea."ExtAuthorID",
    ea."PaperID",
    ea."FullName"                                              AS "ScrapedName",
    u."UserID"                                                 AS "PossibleResearcherID",
    CONCAT(u."FirstName", ' ', u."LastName")                   AS "RegisteredName",
    SIMILARITY(ea."FullName", CONCAT(u."FirstName", ' ', u."LastName")) AS "MatchScore"
FROM "ExternalAuthors" ea
CROSS JOIN "Users" u
INNER JOIN "Researcher" r ON u."UserID" = r."UserID"
WHERE
    SIMILARITY(ea."FullName", CONCAT(u."FirstName", ' ', u."LastName")) > 0.55
    AND u."AccountStatus" = 'Active'
ORDER BY "MatchScore" DESC, ea."PaperID";
"""


# Sanity-check #1: How many papers exist in total?
PAPER_COUNT_SQL = 'SELECT COUNT(*) FROM "ResearchPaper";'

# Sanity-check #2: How many Authors links exist?
AUTHORS_COUNT_SQL = 'SELECT COUNT(*) FROM "Authors";'


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _run(sql: str) -> List[tuple]:
    if connection is None:
        raise RuntimeError("Django connection unavailable. Run inside Django context.")
    with connection.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def find_orphaned_papers() -> List[OrphanedPaper]:
    rows = _run(ORPHANED_PAPERS_SQL)
    return [
        OrphanedPaper(
            paper_id=r[0],
            title=(r[1] or '')[:300],
            source=r[2],
            scraped_at=str(r[3]) if r[3] else '',
            external_authors_count=r[4],
        )
        for r in rows
    ]


def find_possible_matches() -> List[PossibleMatch]:
    rows = _run(POSSIBLE_MATCHES_SQL)
    return [
        PossibleMatch(
            ext_author_id=r[0],
            paper_id=r[1],
            scraped_name=r[2],
            possible_researcher_id=r[3],
            registered_name=r[4],
            match_score=float(r[5]),
        )
        for r in rows
    ]


def write_csv(rows: Iterable, path: str) -> int:
    rows = list(rows)
    if not rows:
        with open(path, 'w', newline='', encoding='utf-8') as f:
            f.write('# No rows found.\n')
        return 0
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
    return len(rows)


def run(output_dir: str = './reports/') -> None:
    os.makedirs(output_dir, exist_ok=True)
    stamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')

    print('━' * 60)
    print(f'  Litrix Author Linkage Diagnostic  ·  {stamp}')
    print('━' * 60)

    # Sanity checks
    total_papers = _run(PAPER_COUNT_SQL)[0][0]
    total_links = _run(AUTHORS_COUNT_SQL)[0][0]
    print(f'  Total papers in DB ............ {total_papers}')
    print(f'  Total Authors links ........... {total_links}')

    # Orphans
    orphans = find_orphaned_papers()
    orphans_path = os.path.join(output_dir, f'orphaned_papers_{stamp}.csv')
    n_orphans = write_csv(orphans, orphans_path)
    print(f'  Orphaned papers found ......... {n_orphans}')
    print(f'    → {orphans_path}')

    # Possible matches
    matches = find_possible_matches()
    matches_path = os.path.join(output_dir, f'possible_matches_{stamp}.csv')
    n_matches = write_csv(matches, matches_path)
    print(f'  Possible matches found ........ {n_matches}')
    print(f'    → {matches_path}')

    # Health verdict
    if total_papers > 0:
        coverage = 100.0 * (total_papers - n_orphans) / total_papers
        print(f'  Linkage coverage .............. {coverage:.2f}%')
    print('━' * 60)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='./reports/', help='Output directory for CSVs')
    args = parser.parse_args()
    run(args.output)
