"""
Backfill missing DOIs on ResearchPaper rows - resilient to connection drops.

DESIGN CHANGE FROM v1
---------------------
v1 collected all DOI discoveries in memory, then wrote them all at the
end inside one transaction. Result: a 2-hour run could be wiped out by
a single Neon connection drop near the end.

v2 commits IMMEDIATELY after each successful lookup:
    for paper in targets:
        doi = lookup(paper.title)
        if doi: UPDATE + COMMIT      <-- writes one row at a time
        on OperationalError: reconnect and continue

Trade-off: more DB round-trips, but the script is now interruptible at
any point with NO data loss, and re-running picks up where it stopped
(idempotent: SELECT already filters out papers with DOI set).

USAGE
-----
    python manage.py backfill_dois --dry-run --limit 100
    python manage.py backfill_dois --limit 500
    python manage.py backfill_dois
"""
import logging
import time

from django.core.management.base import BaseCommand
from django.db import connection, transaction, IntegrityError, OperationalError

from analytics.lookups import paper_by_title

logger = logging.getLogger(__name__)


SELECT_TARGETS_SQL = """
SELECT rp."PaperID", rp."Title", rp."PubYear"
FROM "ResearchPaper" rp
WHERE (rp."DOI" IS NULL OR rp."DOI" = '')
  AND rp."Title" IS NOT NULL
  AND LENGTH(rp."Title") >= 12
  {year_clause}
ORDER BY rp."PaperID"
{limit_clause}
"""

CHECK_DOI_FREE_SQL = """
SELECT 1 FROM "ResearchPaper"
WHERE LOWER("DOI") = LOWER(%s) AND "PaperID" != %s
LIMIT 1
"""

UPDATE_DOI_SQL = """
UPDATE "ResearchPaper"
SET "DOI" = %s
WHERE "PaperID" = %s
  AND ("DOI" IS NULL OR "DOI" = '')
"""


def _safe_execute(sql, params=None, max_retries=3):
    """Execute SQL with reconnect-on-drop. Returns the cursor result rows."""
    for attempt in range(max_retries):
        try:
            with connection.cursor() as cur:
                cur.execute(sql, params or [])
                try:
                    return cur.fetchall()
                except Exception:
                    return None  # not a SELECT
        except OperationalError as exc:
            logger.warning("DB connection dropped (attempt %s/%s): %s",
                           attempt + 1, max_retries, exc)
            connection.close()
            time.sleep(2 ** attempt)  # 1s, 2s, 4s backoff
    raise OperationalError(f"DB unreachable after {max_retries} retries")


class Command(BaseCommand):
    help = "Backfill missing DOIs - resilient to connection drops."

    def add_arguments(self, parser):
        parser.add_argument("--year", type=int, default=0)
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument("--dry-run", action="store_true",
                            help="Run lookups, but skip the actual UPDATE")

    def handle(self, *args, **opts):
        year     = opts["year"]
        limit    = opts["limit"]
        dry_run  = opts["dry_run"]

        year_clause  = 'AND rp."PubYear" = %s' if year else ""
        limit_clause = f"LIMIT {int(limit)}" if limit > 0 else ""
        sql = SELECT_TARGETS_SQL.format(
            year_clause=year_clause, limit_clause=limit_clause
        )
        params = [year] if year else []

        targets = _safe_execute(sql, params)
        self.stdout.write(f"Papers missing DOI: {len(targets)}")
        if not targets:
            return

        pending_dois = set()
        n_found = n_no_match = n_dup_db = n_dup_run = n_written = n_failed = 0

        for paper_id, title, pub_year in targets:

            try:
                paper = paper_by_title(title)
            except Exception as exc:
                logger.debug("lookup failed for paper_id=%s: %s", paper_id, exc)
                n_no_match += 1
                continue

            if not paper or not paper.get("doi"):
                n_no_match += 1
                _maybe_print(self, n_found, n_no_match, n_dup_db, n_dup_run, n_written)
                continue

            doi = paper["doi"].strip()
            doi_lower = doi.lower()

            if doi_lower in pending_dois:
                n_dup_run += 1
                _maybe_print(self, n_found, n_no_match, n_dup_db, n_dup_run, n_written)
                continue

            # Check if some other paper in the DB already owns this DOI.
            try:
                rows = _safe_execute(CHECK_DOI_FREE_SQL, [doi, paper_id])
            except OperationalError as exc:
                self.stdout.write(self.style.ERROR(
                    f"Permanent DB failure on paper_id={paper_id}: {exc}"))
                break
            if rows:
                n_dup_db += 1
                _maybe_print(self, n_found, n_no_match, n_dup_db, n_dup_run, n_written)
                continue

            n_found += 1
            pending_dois.add(doi_lower)

            # Write IMMEDIATELY. Each UPDATE is its own atomic - if it
            # fails (IntegrityError or anything else), we log + move on.
            if dry_run:
                _maybe_print(self, n_found, n_no_match, n_dup_db, n_dup_run, n_written)
                continue

            for attempt in range(3):
                try:
                    with transaction.atomic():
                        with connection.cursor() as cur:
                            cur.execute(UPDATE_DOI_SQL, [doi, paper_id])
                    n_written += 1
                    break
                except IntegrityError as exc:
                    n_failed += 1
                    logger.warning("IntegrityError paper_id=%s doi=%s: %s",
                                   paper_id, doi, exc)
                    break
                except OperationalError as exc:
                    logger.warning("OperationalError on UPDATE attempt %s: %s",
                                   attempt + 1, exc)
                    connection.close()
                    time.sleep(2 ** attempt)
            else:
                n_failed += 1

            _maybe_print(self, n_found, n_no_match, n_dup_db, n_dup_run, n_written)

        self.stdout.write("")
        self.stdout.write(f"  DOIs found        : {n_found}")
        self.stdout.write(f"  No reliable match : {n_no_match}")
        self.stdout.write(f"  Dup in DB         : {n_dup_db}")
        self.stdout.write(f"  Dup in run        : {n_dup_run}")
        self.stdout.write(f"  Updates written   : {n_written}")
        self.stdout.write(f"  Update failures   : {n_failed}")

        if dry_run:
            self.stdout.write(self.style.WARNING(
                "\n--dry-run: lookups only, no writes."))
        else:
            self.stdout.write(self.style.SUCCESS("\nDone."))


def _maybe_print(cmd, found, no_match, dup_db, dup_run, written):
    total = found + no_match + dup_db + dup_run
    if total > 0 and total % 25 == 0:
        cmd.stdout.write(
            f"  ... processed {total} "
            f"(found={found}, no_match={no_match}, "
            f"dup_db={dup_db}, dup_run={dup_run}, written={written})"
        )
