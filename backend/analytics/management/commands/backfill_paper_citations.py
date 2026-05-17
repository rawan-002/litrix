"""
Backfill ResearchPaper.CitationsByYear from OpenAlex via DOI.

For each paper with a DOI but no CitationsByYear, query OpenAlex's
/works/doi:<doi> endpoint and extract `counts_by_year`, then save it
as JSONB to ResearchPaper.CitationsByYear in {"YYYY": count, ...} shape.

USAGE:
    python manage.py backfill_paper_citations --dry-run --limit 50
    python manage.py backfill_paper_citations
"""
import json
import time
import logging

from django.core.management.base import BaseCommand
from django.db import connection, transaction, IntegrityError, OperationalError

import requests

logger = logging.getLogger(__name__)

USER_AGENT = ("Litrix/1.0 (research-analytics; mailto:admin@example.com) "
              "AppleWebKit/537.36")
HEADERS = {"User-Agent": USER_AGENT}
RATE_LIMIT_SECONDS = 0.5


_last_request_at = 0.0
def _rate_limit():
    global _last_request_at
    delta = time.time() - _last_request_at
    if delta < RATE_LIMIT_SECONDS:
        time.sleep(RATE_LIMIT_SECONDS - delta)
    _last_request_at = time.time()


def fetch_counts_by_year(doi):
    """Returns dict {year: count} or None if not available."""
    _rate_limit()
    try:
        r = requests.get(
            f"https://api.openalex.org/works/doi:{doi}",
            headers=HEADERS, timeout=20,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        counts = data.get("counts_by_year") or []
        if not counts:
            return None
        return {str(c["year"]): int(c.get("cited_by_count", 0)) for c in counts}
    except Exception as e:
        logger.debug("OpenAlex lookup failed for %s: %s", doi, e)
        return None


class Command(BaseCommand):
    help = "Backfill ResearchPaper.CitationsByYear from OpenAlex"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        limit_clause = f"LIMIT {int(opts['limit'])}" if opts["limit"] > 0 else ""

        with connection.cursor() as cur:
            cur.execute(f"""
                SELECT "PaperID", "DOI"
                FROM "ResearchPaper"
                WHERE "DOI" IS NOT NULL AND "DOI" <> ''
                  AND ("CitationsByYear" IS NULL
                       OR "CitationsByYear" = '{{}}'::jsonb)
                ORDER BY "PaperID"
                {limit_clause}
            """)
            targets = cur.fetchall()
        self.stdout.write(f"Papers needing CitationsByYear: {len(targets)}")
        if not targets:
            return

        n_found = n_no_data = n_written = n_failed = 0

        for paper_id, doi in targets:
            data = fetch_counts_by_year(doi)
            if not data:
                n_no_data += 1
            else:
                n_found += 1
                if not opts["dry_run"]:
                    for attempt in range(3):
                        try:
                            with transaction.atomic():
                                with connection.cursor() as cur:
                                    cur.execute("""
                                        UPDATE "ResearchPaper"
                                        SET "CitationsByYear" = %s::jsonb
                                        WHERE "PaperID" = %s
                                    """, [json.dumps(data), paper_id])
                            n_written += 1
                            break
                        except OperationalError:
                            connection.close()
                            time.sleep(2 ** attempt)
                        except IntegrityError as e:
                            n_failed += 1
                            logger.warning("paper_id=%s: %s", paper_id, e)
                            break

            total = n_found + n_no_data
            if total > 0 and total % 25 == 0:
                self.stdout.write(
                    f"  ... {total}/{len(targets)} "
                    f"(found={n_found}, no_data={n_no_data}, "
                    f"written={n_written})"
                )

        self.stdout.write("")
        self.stdout.write(f"  Counts found     : {n_found}")
        self.stdout.write(f"  No data in API   : {n_no_data}")
        self.stdout.write(f"  Updates written  : {n_written}")
        self.stdout.write(f"  Failures         : {n_failed}")

        if opts["dry_run"]:
            self.stdout.write(self.style.WARNING(
                "\n--dry-run: nothing written."))
        else:
            self.stdout.write(self.style.SUCCESS("\nDone."))
