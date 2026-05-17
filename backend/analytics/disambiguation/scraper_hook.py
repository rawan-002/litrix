"""
Scraper integration helper
==========================
Drop-in helper to call from the scrapers (scholar.py / orcid.py / scopus.py)
right after a paper is inserted/updated. Wraps `link_coauthors_for_paper`
in defensive error handling so a single-paper hiccup never breaks the
scraping batch.

WHERE TO PLUG IT IN
-------------------
In `scrapers/scholar.py`, look for the block that links the trigger
researcher (around line 397):

    cur.execute('''
        INSERT INTO "Authors" (
            "UserID", "PaperID", ... )
        VALUES (%s, %s, NULL, FALSE, 1.0, 'scholar_id_verified', %s, TRUE)
        ON CONFLICT ("UserID", "PaperID") DO NOTHING
    ''', (user_id, paper_id, scholar_authors_str))
    if cur.rowcount > 0:
        n_linked_new += 1

    # ADD THIS BLOCK ↓↓↓ — process co-authors using disambiguation pipeline
    from analytics.disambiguation.scraper_hook import after_paper_persisted
    after_paper_persisted(cur, paper_id, user_id)
    # ↑↑↑ END ADDITION

The same two-line addition belongs at the equivalent spot in
scrapers/orcid.py and scrapers/scopus.py.

WHY A WRAPPER (NOT JUST IMPORTING THE PIPELINE)
-----------------------------------------------
The wrapper swallows exceptions per-paper and logs them. This matches
how the scrapers handle errors elsewhere (continue the batch, log the
casualty) rather than aborting an hour-long run for one malformed
authorships JSON.
"""
import logging
from typing import Dict

from .pipeline import link_coauthors_for_paper

logger = logging.getLogger(__name__)


def after_paper_persisted(cur, paper_id: int, trigger_user_id: int) -> Dict[str, int]:
    """
    Call after the scraper has:
      1. INSERTed (or UPDATEd) the ResearchPaper row.
      2. INSERTed the trigger researcher's Authors link.
      3. Committed the RawData_Log->'authorships' payload.

    Safe to call repeatedly for the same paper — pipeline uses
    ON CONFLICT DO NOTHING everywhere.
    """
    try:
        return link_coauthors_for_paper(cur, paper_id, trigger_user_id)
    except Exception as exc:
        logger.exception(
            'Co-author linkage failed for paper_id=%s trigger_user_id=%s',
            paper_id, trigger_user_id,
        )
        return {
            'linked':   0,
            'queued':   0,
            'external': 0,
            'skipped':  0,
            'error':    str(exc),
        }
