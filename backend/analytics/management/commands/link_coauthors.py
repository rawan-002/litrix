"""
Backfill command — re-process co-author linkage for existing papers.

WHEN TO RUN
-----------
• Once, immediately after deploying the disambiguation pipeline. This
  retroactively links co-authors on papers scraped before the fix.
• After a Users bulk-import (newly registered researchers won't have been
  matched against historical papers).
• Anytime the SimilarityThreshold or matching tiers change.

WHAT IT DOES
------------
Iterates over every ResearchPaper that has RawData_Log->'authorships',
and runs analytics.disambiguation.pipeline.link_coauthors_for_paper().

Idempotent: the pipeline uses ON CONFLICT DO NOTHING on Authors so re-runs
won't duplicate links. AuthorReviewQueue is protected by the
UNIQUE(PaperID, ScrapedName) constraint.

USAGE
-----
    python manage.py link_coauthors                       # all papers
    python manage.py link_coauthors --since 2024-01-01    # only recent
    python manage.py link_coauthors --user-id 42          # one researcher
    python manage.py link_coauthors --dry-run             # report only
    python manage.py link_coauthors --limit 100           # batch
"""
import logging
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from analytics.disambiguation import link_coauthors_for_paper

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Backfill co-author Authors links for existing ResearchPaper rows.'

    def add_arguments(self, parser):
        parser.add_argument('--since', type=str,
                            help='Only process papers scraped on/after YYYY-MM-DD')
        parser.add_argument('--user-id', type=int,
                            help='Only process papers belonging to this UserID')
        parser.add_argument('--limit', type=int, default=0,
                            help='Stop after processing N papers (0 = unlimited)')
        parser.add_argument('--dry-run', action='store_true',
                            help='Compute counts but ROLLBACK at the end')

    def handle(self, *args, **opts):
        since   = opts.get('since')
        user_id = opts.get('user_id')
        limit   = opts.get('limit') or 0
        dry_run = opts.get('dry_run')

        if since:
            try:
                datetime.strptime(since, '%Y-%m-%d')
            except ValueError:
                raise CommandError('Invalid --since date. Use YYYY-MM-DD.')

        where_parts = ['rp."RawData_Log" IS NOT NULL']
        params = []
        if since:
            where_parts.append('rp."ScrapedAt" >= %s')
            params.append(since)
        if user_id:
            where_parts.append('EXISTS (SELECT 1 FROM "Authors" a '
                               'WHERE a."PaperID" = rp."PaperID" AND a."UserID" = %s)')
            params.append(user_id)
        where_sql = ' AND '.join(where_parts)

        limit_sql = f'LIMIT {int(limit)}' if limit > 0 else ''

        # We pick the "trigger" researcher per paper as the Authors row with
        # the highest MappingConfidence (typically the scholar_id_verified
        # one written by the scraper). That row's UserID is the one we skip
        # in the pipeline so we don't re-write it.
        select_sql = f'''
            SELECT
                rp."PaperID",
                COALESCE(
                    (SELECT a."UserID"
                     FROM "Authors" a
                     WHERE a."PaperID" = rp."PaperID"
                     ORDER BY a."MappingConfidence" DESC NULLS LAST,
                              a."AuthorOrder"        ASC  NULLS LAST
                     LIMIT 1),
                    0
                ) AS trigger_user_id
            FROM "ResearchPaper" rp
            WHERE {where_sql}
            ORDER BY rp."PaperID"
            {limit_sql}
        '''

        totals = {'papers': 0, 'linked': 0, 'queued': 0, 'external': 0, 'skipped': 0}

        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(select_sql, params)
                paper_rows = cur.fetchall()

                for paper_id, trigger_uid in paper_rows:
                    stats = link_coauthors_for_paper(cur, paper_id, trigger_uid or 0)
                    totals['papers']   += 1
                    totals['linked']   += stats['linked']
                    totals['queued']   += stats['queued']
                    totals['external'] += stats['external']
                    totals['skipped']  += stats['skipped']

                    if totals['papers'] % 50 == 0:
                        self.stdout.write(
                            f"  processed {totals['papers']} papers · "
                            f"linked={totals['linked']} queued={totals['queued']} "
                            f"external={totals['external']}"
                        )

            if dry_run:
                # Rollback by raising — we're inside `transaction.atomic`.
                self.stdout.write(self.style.WARNING('--dry-run: rolling back.'))
                transaction.set_rollback(True)

        self.stdout.write(self.style.SUCCESS('━' * 60))
        self.stdout.write(self.style.SUCCESS(
            f"  Papers scanned       : {totals['papers']}\n"
            f"  Co-authors linked    : {totals['linked']}   (high confidence)\n"
            f"  Queued for review    : {totals['queued']}   (admin to decide)\n"
            f"  External authors     : {totals['external']} (no Litrix match)\n"
            f"  Skipped (trigger)    : {totals['skipped']}"
        ))
        self.stdout.write(self.style.SUCCESS('━' * 60))
