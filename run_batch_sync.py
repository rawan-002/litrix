"""
Litrix Batch Sync Orchestrator
===============================
Drives `run_full_sync` over every Researcher with a Scholar_ID, with safe
resume-on-failure and rate limiting between researchers.

Why a separate file? `litrix_scraper.py` is the single-researcher pipeline
(unit of work). This file is the orchestrator (unit of execution). Keeping
them separate means the per-researcher logic stays testable and reusable
(e.g., for an Admin "Re-Sync" button later in the Angular dashboard).

Resume semantics:
    Each successful sync stamps Researcher.LastSyncedAt = NOW(). On a
    re-run, researchers synced within --min-age-hours are skipped. So if
    the batch crashes after researcher #20 of 59, re-running skips the
    first 20 and resumes from #21.

Rate limiting:
    The per-paper delay (0.4-1.0s) lives inside run_full_sync. This
    orchestrator adds a separate inter-researcher delay (default 3-6s)
    to spread the load on SerpAPI/CrossRef/OpenAlex and avoid burst
    detection.

CLI:
    python run_batch_sync.py                        # full batch
    python run_batch_sync.py --dry-run              # list, don't execute
    python run_batch_sync.py --max 5                # cap at 5 researchers
    python run_batch_sync.py --min-age-hours 0      # ignore freshness gate
    python run_batch_sync.py --min-age-hours 168    # weekly re-sync
"""

import os
import sys
import time
import random
import logging
import argparse
from typing import List, Tuple, Optional, Dict

import psycopg2
from dotenv import load_dotenv

from litrix_scraper import (
    run_full_sync,
    run_full_sync_via_orcid,
    run_full_sync_via_scopus,
    run_full_sync_via_rg,
)


load_dotenv()

DB_CONFIG = {
    "dbname":   os.getenv("DB_NAME", "LitrixDB"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD"),
    "host":     os.getenv("DB_HOST", "localhost"),
    "port":     os.getenv("DB_PORT", "5432"),
}

LOG_FILE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "batch_sync.log"
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE_PATH, mode='w', encoding='utf-8'),
        logging.StreamHandler(),
    ]
)


def get_researchers_to_sync(
    min_age_hours: int
) -> List[Tuple[int, Optional[str], Optional[str], Optional[str], Optional[str], str]]:
    """
    Return the ordered list of researchers eligible for sync. A researcher
    is eligible if they have AT LEAST ONE source identifier, in priority:
        1. Scholar_ID         → SerpAPI/Scholar pipeline (richest data)
        2. ORCID_ID           → OpenAlex via ORCID       (deterministic)
        3. Scopus_ID          → OpenAlex via Scopus      (deterministic)
        4. ResearchGate_URL   → OpenAlex via name search (lowest confidence)

    Tuple shape: (UserID, Scholar_ID, ORCID_ID, Scopus_ID, RG_URL, name).
    The dispatcher in run_batch() walks this priority order top-down.

    Order across researchers: alphabetical by FullName_Ar (deterministic).
    """
    sql = '''
        SELECT
            u."UserID",
            u."Scholar_ID",
            r."ORCID_ID",
            r."Scopus_ID",
            r."ResearchGate_URL",
            COALESCE(u."FullName_Ar", u."FirstName" || ' ' || u."LastName")
                AS display_name
        FROM "Users" u
        JOIN "Researcher" r ON r."UserID" = u."UserID"
        WHERE (
                u."Scholar_ID"        IS NOT NULL
             OR r."ORCID_ID"          IS NOT NULL
             OR r."Scopus_ID"         IS NOT NULL
             OR r."ResearchGate_URL"  IS NOT NULL
          )
          AND (
              r."LastSyncedAt" IS NULL
              OR r."LastSyncedAt" < NOW() - make_interval(hours => %s)
          )
        ORDER BY u."FullName_Ar" NULLS LAST, u."UserID"
    '''
    with psycopg2.connect(**DB_CONFIG) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (min_age_hours,))
            return cur.fetchall()


def aggregate_stats(per_researcher: List[Optional[Dict[str, int]]]) -> Dict[str, int]:
    """Sum per-researcher stats dicts into a single batch-level report."""
    total = {"new": 0, "existing": 0, "enriched": 0, "error": 0,
             "researchers_ok": 0, "researchers_failed": 0}
    for stats in per_researcher:
        if stats is None:
            total["researchers_failed"] += 1
        else:
            total["researchers_ok"] += 1
            for k in ("new", "existing", "enriched", "error"):
                total[k] += stats.get(k, 0)
    return total


def run_batch(min_age_hours: int, max_researchers: Optional[int],
              dry_run: bool, sleep_min: float, sleep_max: float) -> None:
    queue = get_researchers_to_sync(min_age_hours)
    total_eligible = len(queue)

    if max_researchers is not None and max_researchers > 0:
        queue = queue[:max_researchers]

    logging.info(
        f"Eligible researchers: {total_eligible}"
        + (f" (capped at {len(queue)})" if max_researchers else "")
    )

    def _pick_source(sid, orcid, scopus, rg):
        """
        Apply priority order and return (label, callable) or (None, None).
        Priority: Scholar > ORCID > Scopus > ResearchGate.
        """
        if sid:
            return f"Scholar={sid}", lambda: run_full_sync(sid)
        if orcid:
            return f"ORCID={orcid}", lambda: run_full_sync_via_orcid(orcid)
        if scopus:
            return f"Scopus={scopus}", lambda: run_full_sync_via_scopus(scopus)
        if rg:
            return f"RG={rg}", lambda: run_full_sync_via_rg(rg)
        return None, None

    if dry_run:
        print("\n--- DRY RUN: would sync the following researchers ---")
        for i, (uid, sid, orcid, scopus, rg, name) in enumerate(queue, 1):
            label, _ = _pick_source(sid, orcid, scopus, rg)
            print(f"  {i:3d}. {name}  (UserID={uid}, via {label or 'NONE'})")
        print(f"\nTotal: {len(queue)}")
        return

    if not queue:
        logging.info("Nothing to sync — every researcher is fresh.")
        return

    per_researcher_stats: List[Optional[Dict[str, int]]] = []

    for i, (uid, sid, orcid, scopus, rg, name) in enumerate(queue, 1):
        logging.info("")
        source_label, sync_fn = _pick_source(sid, orcid, scopus, rg)
        if sync_fn is None:
            logging.warning(
                f"  ✗ {name}: no Scholar/ORCID/Scopus/RG identifier, skipped"
            )
            per_researcher_stats.append(None)
            continue

        logging.info(f"━━━ [{i}/{len(queue)}] {name}  ({source_label}) ━━━")
        try:
            stats = sync_fn()
            per_researcher_stats.append(stats)
            if stats:
                logging.info(f"  ✓ {stats}")
            else:
                logging.warning(f"  ✗ early failure (no articles or API issue)")
        except Exception as e:
            logging.exception(f"  ✗ unhandled error: {e}")
            per_researcher_stats.append(None)

        if i < len(queue):
            delay = random.uniform(sleep_min, sleep_max)
            logging.info(f"  … sleeping {delay:.1f}s before next researcher")
            time.sleep(delay)

    summary = aggregate_stats(per_researcher_stats)
    logging.info("")
    logging.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logging.info("BATCH SYNC COMPLETE")
    logging.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logging.info(f"  Researchers OK     : {summary['researchers_ok']}")
    logging.info(f"  Researchers failed : {summary['researchers_failed']}")
    logging.info(f"  Papers new         : {summary['new']}")
    logging.info(f"  Papers enriched    : {summary['enriched']}")
    logging.info(f"  Papers existing    : {summary['existing']}")
    logging.info(f"  Paper-level errors : {summary['error']}")
    logging.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Litrix batch sync: scrape every Researcher with a Scholar_ID."
    )
    parser.add_argument(
        "--min-age-hours", type=int, default=24,
        help="Skip researchers synced within the last N hours (default: 24)."
    )
    parser.add_argument(
        "--max", type=int, default=None, dest="max_researchers",
        help="Cap the run at N researchers (useful for first-run testing)."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List who would be synced without running the scraper."
    )
    parser.add_argument(
        "--sleep-min", type=float, default=3.0,
        help="Minimum inter-researcher cooldown in seconds (default: 3)."
    )
    parser.add_argument(
        "--sleep-max", type=float, default=6.0,
        help="Maximum inter-researcher cooldown in seconds (default: 6)."
    )
    args = parser.parse_args()

    if args.sleep_min > args.sleep_max:
        parser.error("--sleep-min cannot exceed --sleep-max")

    run_batch(
        min_age_hours=args.min_age_hours,
        max_researchers=args.max_researchers,
        dry_run=args.dry_run,
        sleep_min=args.sleep_min,
        sleep_max=args.sleep_max,
    )


if __name__ == "__main__":
    main()
