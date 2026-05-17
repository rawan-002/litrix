"""
Enrich Quartile + SJR + IF for Journals using Scimago.

WHAT IT DOES
------------
For each Journal in the Journals table that has no JournalRankings row
for the target year (default: current year), look it up in Scimago by
ISSN first, then by name. If found, insert a JournalRankings row with:
    Source          = 'scimago'
    Quartile        = best SJR quartile (Q1-Q4)
    ImpactFactor    = SJR score (Scimago does not publish JCR IF; SJR is
                      the closest comparable metric)
    RankingYear     = --year (default: current year)

WHY THIS WAY
------------
1. JournalRankings already has a unique on (JournalID, RankingYear, Source)
   in your migrations, so ON CONFLICT DO NOTHING keeps it idempotent.
2. We never overwrite an existing ranking - re-runs are safe.
3. Every insert is wrapped in transaction.atomic + supports --dry-run.

USAGE
-----
    python manage.py enrich_quartiles --dry-run
    python manage.py enrich_quartiles
    python manage.py enrich_quartiles --year 2024 --limit 100
"""
from datetime import datetime
import logging

from django.core.management.base import BaseCommand
from django.db import connection, transaction

from analytics.lookups import journal_by_issn, journal_by_name

logger = logging.getLogger(__name__)


CURRENT_YEAR = datetime.now().year


SELECT_TARGETS_SQL = """
SELECT
    j."JournalID",
    j."JournalName",
    j."ISSN_Print",
    j."ISSN_Online"
FROM "Journals" j
WHERE NOT EXISTS (
    SELECT 1 FROM "JournalRankings" jr
    WHERE jr."JournalID" = j."JournalID"
      AND jr."RankingYear" = %s
      AND jr."Source" = 'scimago'
)
ORDER BY j."JournalID"
"""


INSERT_RANKING_SQL = """
INSERT INTO "JournalRankings" (
    "JournalID", "RankingYear", "Source",
    "Quartile", "ImpactFactor"
)
VALUES (%s, %s, 'scimago', %s, %s)
ON CONFLICT DO NOTHING
"""


def _normalize_quartile(q):
    """Scimago 'Q1'/'Q2'/'Q3'/'Q4' or '-' for unranked."""
    if not q:
        return None
    q = q.strip().upper()
    return q if q in ("Q1", "Q2", "Q3", "Q4") else None


def _to_float(val):
    if val is None or val == "":
        return None
    try:
        return float(str(val).replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


class Command(BaseCommand):
    help = "Enrich JournalRankings with Quartile + SJR data from Scimago."

    def add_arguments(self, parser):
        parser.add_argument("--year", type=int, default=CURRENT_YEAR,
                            help=f"Ranking year (default {CURRENT_YEAR})")
        parser.add_argument("--limit", type=int, default=0,
                            help="Process at most N journals (0 = unlimited)")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        year    = opts["year"]
        limit   = opts["limit"]
        dry_run = opts["dry_run"]

        with connection.cursor() as cur:
            cur.execute(SELECT_TARGETS_SQL, [year])
            targets = cur.fetchall()
        if limit > 0:
            targets = targets[:limit]
        self.stdout.write(f"Journals needing enrichment for {year}: {len(targets)}")
        if not targets:
            return

        n_matched_issn = 0
        n_matched_name = 0
        n_not_found    = 0
        rows_to_insert = []

        for jid, jname, issn_p, issn_o in targets:
            issns = [i for i in (issn_p, issn_o) if i]
            data  = journal_by_issn(issns)
            if data:
                n_matched_issn += 1
            else:
                data = journal_by_name(jname)
                if data:
                    n_matched_name += 1
                else:
                    n_not_found += 1
                    continue

            quartile = _normalize_quartile(data.get("best_quartile"))
            sjr      = _to_float(data.get("sjr"))
            rows_to_insert.append((jid, year, quartile, sjr))

            if (n_matched_issn + n_matched_name) % 25 == 0:
                self.stdout.write(
                    f"  ... {n_matched_issn + n_matched_name} matched, "
                    f"{n_not_found} not found"
                )

        self.stdout.write("")
        self.stdout.write(f"  Matched via ISSN : {n_matched_issn}")
        self.stdout.write(f"  Matched via name : {n_matched_name}")
        self.stdout.write(f"  Not found        : {n_not_found}")
        self.stdout.write(f"  Rows to insert   : {len(rows_to_insert)}")

        with transaction.atomic():
            with connection.cursor() as cur:
                for row in rows_to_insert:
                    cur.execute(INSERT_RANKING_SQL, row)

            if dry_run:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING(
                    "\n--dry-run: rolled back. Nothing written."))
            else:
                self.stdout.write(self.style.SUCCESS(
                    "\nDone. JournalRankings updated."))
