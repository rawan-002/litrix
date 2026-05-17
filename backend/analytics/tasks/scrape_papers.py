"""
Litrix — Paper Scraping Celery Task
====================================
Orchestrates the end-to-end flow:
    RegistrationRequest approved
        └→ Celery task fires
            └→ scraper pulls papers from GoogleScholar_URL
                └→ each paper passes through the Disambiguation Pipeline
                    └→ Authors links written + AuditLog entry + ScrapedAt stamp

WHY A SEPARATE TASK MODULE
--------------------------
We isolate the I/O layer (network, DB writes, logging) from the pure
disambiguation logic in `disambiguation/pipeline.py`. This keeps each layer
unit-testable in isolation.
"""

from __future__ import annotations

import logging
from typing import List

# from celery import shared_task
# from django.db import transaction
# from django.utils import timezone
# from analytics.models import (
#     RegistrationRequest, Researcher, ResearchPaper, AuditLog
# )
from analytics.disambiguation.pipeline import (
    ScrapedAuthor,
    process_paper_authors,
)
# from analytics.scrapers.scholar import ScholarScraper

logger = logging.getLogger(__name__)


# @shared_task(
#     bind=True,
#     name='analytics.scrape_researcher_papers',
#     autoretry_for=(ConnectionError, TimeoutError),
#     retry_backoff=True,
#     max_retries=3,
# )
def scrape_researcher_papers(self, researcher_user_id: int) -> dict:
    """
    Main scraping task. Triggered by post_save signal on RegistrationRequest
    when its Status transitions to 'Approved'.

    Flow:
        1. Load Researcher + GoogleScholar_URL.
        2. Pull all papers from Scholar profile.
        3. For each paper:
             a. Upsert into ResearchPaper (dedup by DOI/Title).
             b. Call process_paper_authors() to handle linkage.
             c. Stamp ScrapedAt.
        4. Write AuditLog entry summarizing the run.
    """
    # researcher = Researcher.objects.select_related('user').get(UserID=researcher_user_id)
    # if not researcher.GoogleScholar_URL:
    #     logger.warning(f'No GoogleScholar_URL for researcher {researcher_user_id}')
    #     return {'status': 'skipped', 'reason': 'no_scholar_url'}

    # scraped_papers = ScholarScraper(researcher.GoogleScholar_URL).fetch_all()

    summary = {
        'researcher_user_id': researcher_user_id,
        'papers_created':     0,
        'papers_updated':     0,
        'total_linked':       0,
        'total_external':     0,
        'queued_for_review':  0,
        'errors':             [],
    }

    # for raw_paper in scraped_papers:
    #     try:
    #         with transaction.atomic():
    #             paper, created = _upsert_paper(raw_paper)
    #             scraped_authors = _build_scraped_authors(raw_paper['authors'])
    #
    #             stats = process_paper_authors(
    #                 paper_id=paper.PaperID,
    #                 trigger_user_id=researcher_user_id,
    #                 scraped_authors=scraped_authors,
    #             )
    #             paper.ScrapedAt = timezone.now()
    #             paper.save(update_fields=['ScrapedAt'])
    #
    #             if created:
    #                 summary['papers_created'] += 1
    #             else:
    #                 summary['papers_updated'] += 1
    #
    #             summary['total_linked']      += stats['linked_internal']
    #             summary['total_external']    += stats['linked_external']
    #             summary['queued_for_review'] += stats['queued_for_review']
    #
    #     except Exception as e:
    #         logger.exception(f'Failed to process paper for researcher {researcher_user_id}')
    #         summary['errors'].append(str(e))

    # _write_audit_log(researcher_user_id, summary)

    return summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_scraped_authors(raw_authors: List[dict]) -> List[ScrapedAuthor]:
    """Convert raw scraper dicts into typed ScrapedAuthor DTOs."""
    return [
        ScrapedAuthor(
            full_name        = a.get('name', '').strip(),
            affiliation      = a.get('affiliation'),
            orcid            = a.get('orcid'),
            scopus_id        = a.get('scopus_id'),
            author_order     = a.get('order'),
            is_corresponding = a.get('is_corresponding', False),
        )
        for a in raw_authors
    ]


def _write_audit_log(researcher_user_id: int, summary: dict) -> None:
    """Persist a row in AuditLogs for traceability."""
    # AuditLog.objects.create(
    #     UserID=researcher_user_id,
    #     Action='SCRAPE_COMPLETED',
    #     EntityName='Researcher',
    #     EntityID=researcher_user_id,
    #     NewValue=summary,
    #     CreatedAt=timezone.now(),
    # )
    logger.info(f'Scrape summary: {summary}')
