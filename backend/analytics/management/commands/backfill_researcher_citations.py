"""
Backfill Researcher.CitationsByYear from OpenAlex via OpenAlex_AuthorID.

For each researcher with OpenAlex_AuthorID but NULL Researcher.CitationsByYear,
query /authors/<id> and extract counts_by_year (author-level aggregate).
"""
import json
import time
import logging

from django.core.management.base import BaseCommand
from django.db import connection, transaction, OperationalError

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


def fetch_author_counts(openalex_id):
    _rate_limit()
    try:
        r = requests.get(
            f"https://api.openalex.org/authors/{openalex_id}",
            headers=HEADERS, timeout=20,
        )
        if r.status_code != 200:
            return None
        counts = r.json().get("counts_by_year") or []
        if not counts:
            return None
        return {str(c["year"]): int(c.get("cited_by_count", 0)) for c in counts}
    except Exception as e:
        logger.debug("OpenAlex author lookup failed for %s: %s", openalex_id, e)
        return None


class Command(BaseCommand):
    help = "Backfill Researcher.CitationsByYear from OpenAlex"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        with connection.cursor() as cur:
            cur.execute("""
                SELECT r."UserID", r."OpenAlex_AuthorID", u."FullName_Ar"
                FROM "Researcher" r
                JOIN "Users" u ON u."UserID" = r."UserID"
                WHERE r."OpenAlex_AuthorID" IS NOT NULL
                  AND r."OpenAlex_AuthorID" <> ''
                  AND (r."CitationsByYear" IS NULL
                       OR r."CitationsByYear" = '{}'::jsonb)
                ORDER BY r."UserID"
            """)
            targets = cur.fetchall()
        self.stdout.write(f"Researchers needing CitationsByYear: {len(targets)}")
        if not targets:
            return

        n_found = n_no_data = n_written = 0
        for user_id, oa_id, name in targets:
            data = fetch_author_counts(oa_id)
            if not data:
                n_no_data += 1
                self.stdout.write(f"  no data: UserID={user_id} ({name})")
                continue
            n_found += 1
            if not opts["dry_run"]:
                with transaction.atomic():
                    with connection.cursor() as cur:
                        cur.execute("""
                            UPDATE "Researcher"
                            SET "CitationsByYear" = %s::jsonb,
                                "LastSyncedAt" = NOW()
                            WHERE "UserID" = %s
                        """, [json.dumps(data), user_id])
                n_written += 1

        self.stdout.write("")
        self.stdout.write(f"  Counts found    : {n_found}")
        self.stdout.write(f"  No data in API  : {n_no_data}")
        self.stdout.write(f"  Updates written : {n_written}")

        if opts["dry_run"]:
            self.stdout.write(self.style.WARNING(
                "\n--dry-run: nothing written."))
        else:
            self.stdout.write(self.style.SUCCESS("\nDone."))
