"""
Backfill CitationsByYear from OpenAlex
=======================================
Walks every paper that has a DOI, re-fetches it from OpenAlex, and
stores `counts_by_year` into ResearchPaper.CitationsByYear.

Why? Our scraper stores only the cumulative cited_by_count. To answer
the question "how many citations did this paper receive IN 2025?", we
need the per-year breakdown that OpenAlex maintains internally but our
pipeline was discarding.

Re-fetching is the cheapest way to get it: OpenAlex is free, and we
only ask once per paper. Total time for ~3,000 papers ≈ 30-40 min.

Resume semantics: a paper with non-NULL CitationsByYear is skipped on
re-runs, so this script is safe to interrupt and restart.
"""

import os
import time
import random
import logging
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import httpx
from dotenv import load_dotenv

load_dotenv()

DB_CONFIG = {
    "dbname":   os.getenv("DB_NAME", "LitrixDB"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD"),
    "host":     os.getenv("DB_HOST", "localhost"),
    "port":     os.getenv("DB_PORT", "5432"),
}

OPENALEX_BASE_URL = "https://api.openalex.org"
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "ra20awn@gmail.com")
HEADERS = {"User-Agent": f"Litrix/1.0 (mailto:{CONTACT_EMAIL})"}
TIMEOUT = 30.0

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "backfill_citations_by_year.log")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_PATH, mode='w', encoding='utf-8'),
        logging.StreamHandler(),
    ]
)


@contextmanager
def db_connection():
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        yield conn
    finally:
        conn.close()


def normalize_doi(doi: str) -> str:
    """Strip URL prefix from DOI for OpenAlex lookup."""
    d = (doi or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(prefix):
            d = d[len(prefix):]
            break
    return d


def fetch_counts_by_year(doi: str) -> dict | None:
    """
    Fetch counts_by_year from OpenAlex for a DOI. Returns a dict like
    {"2024": 15, "2025": 30} or None on miss.
    """
    n = normalize_doi(doi)
    if not n:
        return None
    url = f"{OPENALEX_BASE_URL}/works/doi:{n}"
    try:
        r = httpx.get(
            url,
            headers=HEADERS,
            params={"mailto": CONTACT_EMAIL},
            timeout=TIMEOUT,
            follow_redirects=True,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        cby = data.get('counts_by_year') or []
        if not cby:
            return {}
        return {
            str(item['year']): int(item.get('cited_by_count') or 0)
            for item in cby
            if item.get('year') is not None
        }
    except (httpx.HTTPError, ValueError, KeyError) as e:
        logging.warning(f"OpenAlex miss for DOI '{n}': {e}")
        return None


def get_papers_to_backfill(cur):
    """All papers with a DOI but no CitationsByYear yet."""
    cur.execute('''
        SELECT "PaperID", "DOI", LEFT("Title", 60) AS title_short
        FROM "ResearchPaper"
        WHERE "DOI" IS NOT NULL
          AND "CitationsByYear" IS NULL
        ORDER BY "PaperID"
    ''')
    return cur.fetchall()


def main():
    logging.info("=== Citations-by-year backfill starting ===")

    with db_connection() as conn:
        with conn.cursor() as cur:
            papers = get_papers_to_backfill(cur)

        total = len(papers)
        logging.info(f"Found {total} papers needing backfill")

        if total == 0:
            logging.info("Nothing to backfill — every paper already has data.")
            return

        stats = {"filled": 0, "empty": 0, "miss": 0, "errors": 0}

        for i, (paper_id, doi, title) in enumerate(papers, 1):
            try:
                cby = fetch_counts_by_year(doi)
                if cby is None:
                    stats["miss"] += 1
                    tag = "MISS"
                elif not cby:
                    stats["empty"] += 1
                    tag = "EMPTY"
                else:
                    stats["filled"] += 1
                    tag = f"FILL ({sum(cby.values())} total)"

                if cby is not None:
                    with conn.cursor() as cur:
                        cur.execute('''
                            UPDATE "ResearchPaper"
                            SET "CitationsByYear" = %s::jsonb
                            WHERE "PaperID" = %s
                        ''', (psycopg2.extras.Json(cby), paper_id))
                    conn.commit()

                if i % 20 == 0 or i == total:
                    logging.info(
                        f"  [{i}/{total}] [{tag}] PaperID={paper_id} "
                        f"| {title}..."
                    )
            except Exception as e:
                stats["errors"] += 1
                logging.error(f"  [ERROR] PaperID={paper_id} :: {e}")
                conn.rollback()

            time.sleep(random.uniform(0.2, 0.5))

    logging.info(f"=== Backfill done: {stats} ===")


if __name__ == "__main__":
    main()
