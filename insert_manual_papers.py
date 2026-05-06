"""
Manual Paper Entry Script
==========================
For researchers who have NO public profile (no Scholar, ORCID, Scopus,
RG, DBLP) but whose papers we know about from CVs, personal records, or
direct submission.

Why a script (not raw SQL)?
    1. Idempotent — re-running won't double-insert (DOI/title dedup).
    2. Clean journal handling — same Journal row reused across papers.
    3. Source-tagged — papers are stamped Source='Manual' so the
       Dashboard can distinguish them from auto-scraped data.
    4. Reusable — edit USER_ID and PAPERS, run again for next researcher.

How to use:
    1. Find the researcher's UserID (SQL):
         SELECT "UserID" FROM "Users" WHERE "FullName_Ar" LIKE '%الخاتم%';
    2. Edit USER_ID and PAPERS below.
    3. Run:  python insert_manual_papers.py
    4. The script reports new/skipped counts and stamps LastSyncedAt.
"""

import os
from datetime import datetime
from typing import Optional, Dict, List

import psycopg2
from dotenv import load_dotenv

load_dotenv()

DB_CONFIG = {
    "dbname":   os.getenv("DB_NAME", "LitrixDB"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD"),
    "host":     os.getenv("DB_HOST", "localhost"),
    "port":     os.getenv("DB_PORT", "5432"),
}


USER_ID: int = 25

PAPERS: List[Dict] = [
    {
        "title":     "IoT Guard: An Efficient Intrusion Detection System for IoT "
                     "Using Optimized Feature Selection and Compact Traffic Representation",
        "year":      2026,
        "journal":   "Al-Baha University Journal of Basic and Applied Sciences",
        "publisher": "University of Al-Baha",
        "indexing":  "Scopus",
    },
    {
        "title":     "The Use of Artificial Intelligence in Healthcare in Medical Image Processing",
        "year":      2024,
        "journal":   "International Journal of Computer Science & Network Security (IJCSNS)",
        "indexing":  "ISI IJCSNS",
    },
    {
        "title":     "The Feasibility of Using Random Administrative Samples "
                     "(The French Experiment) in Population Censuses in African Countries",
        "year":      2023,
        "journal":   "International Journal of Current Science Research and Review",
        "volume":    "6",
        "issue":     "2",
        "doi":       "10.47191/ijcsrr/V6-i2-93",
        "issn":      "25818341",
        "indexing":  "ISI IJCSNS",
    },
    {
        "title":     "A Study on Secure Wireless Mobile Data Exchange System in "
                     "Healthcare Using RFID (Radio-Frequency Identification)",
        "year":      2023,
        "journal":   "International Journal of Current Science Research and Review",
        "volume":    "6",
        "issue":     "2",
        "doi":       "10.47191/ijcsrr/V6-i2-00",
        "issn":      "25818341",
        "indexing":  "ISI IJCSNS",
    },
    {
        "title":     "View and evaluate the results of the comprehensive health survey "
                     "with the results of the fifth population census in the indicators "
                     "of the MDG's using geographic information systems",
        "year":      2016,
        "journal":   "Economic Commission for Africa, African Statistical Newsletter",
        "volume":    "8",
        "issue":     "1",
        "indexing":  "Scopus",
    },
]


import re


def normalize_title(title: str) -> str:
    """Same normalization as litrix_scraper for consistent dedup."""
    if not title:
        return ""
    t = title.lower().strip()
    t = re.sub(r'[^\w\s]', ' ', t)
    t = re.sub(r'\s+', ' ', t)
    return t.strip()


def determine_venue_type(name: str) -> str:
    keywords = ('conference', 'proceedings', 'symposium', 'workshop', 'meeting')
    return 'Conference' if any(kw in (name or '').lower() for kw in keywords) else 'Journal'


def upsert_journal(cur, name: str, issn: Optional[str] = None) -> int:
    """Get-or-create a journal. Same logic as the scraper for consistency."""
    name = (name or "Unknown Venue").strip()[:500]
    venue_type = determine_venue_type(name)

    if issn:
        cur.execute(
            'SELECT "JournalID" FROM "Journals" WHERE "ISSN_Print" = %s LIMIT 1',
            (issn,)
        )
        res = cur.fetchone()
        if res:
            return res[0]

    cur.execute(
        'SELECT "JournalID" FROM "Journals" WHERE "JournalName" ILIKE %s LIMIT 1',
        (name,)
    )
    res = cur.fetchone()
    if res:
        return res[0]

    cur.execute('''
        INSERT INTO "Journals" ("JournalName", "ISSN_Print", "VenueType")
        VALUES (%s, %s, %s)
        RETURNING "JournalID"
    ''', (name, issn, venue_type))
    return cur.fetchone()[0]


def find_existing_paper(cur, doi: Optional[str], normalized: str) -> Optional[int]:
    """Two-stage dedup: DOI > NormalizedTitle (same as scraper)."""
    if doi:
        cur.execute('SELECT "PaperID" FROM "ResearchPaper" WHERE "DOI" = %s', (doi,))
        res = cur.fetchone()
        if res:
            return res[0]
    cur.execute(
        'SELECT "PaperID" FROM "ResearchPaper" WHERE "NormalizedTitle" = %s',
        (normalized,)
    )
    res = cur.fetchone()
    return res[0] if res else None


def insert_paper(cur, paper: Dict) -> int:
    """Insert a manual paper, returning the new PaperID."""
    title = paper["title"]
    normalized = normalize_title(title)
    journal_id = upsert_journal(cur, paper.get("journal"), paper.get("issn"))

    raw_log = {
        "manual_entry":      True,
        "manual_entered_at": datetime.utcnow().isoformat(),
        "indexing":          paper.get("indexing"),
        "publisher":         paper.get("publisher"),
        "issn":              paper.get("issn"),
    }

    raw_indexing = (paper.get("indexing") or "").strip()
    indexing_clean = None
    if raw_indexing:
        upper = raw_indexing.upper()
        if "SCOPUS" in upper:
            indexing_clean = "Scopus"
        elif "ISI" in upper or "WOS" in upper or "WEB OF SCIENCE" in upper:
            indexing_clean = "ISI"
        elif "DOAJ" in upper:
            indexing_clean = "DOAJ"
        else:
            indexing_clean = raw_indexing[:50]

    cur.execute('''
        INSERT INTO "ResearchPaper" (
            "JournalID", "Title", "Title_En", "NormalizedTitle",
            "Language", "DOI", "PubYear",
            "Volume", "Issue", "Indexing",
            "IsVerified", "ScrapedAt", "Source", "RawData_Log"
        )
        VALUES (%s, %s, %s, %s, 'en', %s, %s, %s, %s, %s,
                TRUE, NOW(), 'Manual', %s::jsonb)
        RETURNING "PaperID"
    ''', (
        journal_id,
        title,
        title,
        normalized,
        paper.get("doi"),
        paper.get("year"),
        paper.get("volume"),
        paper.get("issue"),
        indexing_clean,
        psycopg2.extras.Json(raw_log) if hasattr(psycopg2, 'extras') else str(raw_log),
    ))
    return cur.fetchone()[0]


def link_author(cur, user_id: int, paper_id: int, author_order: int = 1) -> None:
    """Link the researcher to the paper as primary author."""
    cur.execute('''
        INSERT INTO "Authors" (
            "UserID", "PaperID", "AuthorOrder",
            "IsCorrespondingAuthor", "MappingConfidence", "MappingCriteria",
            "AuthorNameRaw", "Is_Verified"
        )
        VALUES (%s, %s, %s, FALSE, 1.0, 'Manual_Entry', NULL, TRUE)
        ON CONFLICT ("UserID", "PaperID") DO NOTHING
    ''', (user_id, paper_id, author_order))


def stamp_last_synced(cur, user_id: int) -> None:
    cur.execute(
        'UPDATE "Researcher" SET "LastSyncedAt" = NOW() WHERE "UserID" = %s',
        (user_id,)
    )


def main():
    if not USER_ID:
        print("ERROR: set USER_ID at the top of this file first.")
        return
    if not PAPERS:
        print("ERROR: PAPERS list is empty.")
        return

    import psycopg2.extras

    print(f"\nManual paper entry for UserID={USER_ID}")
    print(f"Papers to process: {len(PAPERS)}")
    print("-" * 60)

    stats = {"new": 0, "existing": 0, "linked": 0, "errors": 0}

    with psycopg2.connect(**DB_CONFIG) as conn:
        with conn.cursor() as cur:
            cur.execute(
                'SELECT "FullName_Ar" FROM "Users" WHERE "UserID" = %s',
                (USER_ID,)
            )
            row = cur.fetchone()
            if not row:
                print(f"FATAL: No user with UserID={USER_ID}")
                return
            print(f"Researcher: {row[0]}\n")

        for paper in PAPERS:
            title = paper["title"][:60]
            try:
                with conn.cursor() as cur:
                    doi = paper.get("doi")
                    normalized = normalize_title(paper["title"])
                    existing_id = find_existing_paper(cur, doi, normalized)

                    if existing_id:
                        paper_id = existing_id
                        stats["existing"] += 1
                        tag = "EXISTS"
                    else:
                        paper_id = insert_paper(cur, paper)
                        stats["new"] += 1
                        tag = "NEW   "

                    link_author(cur, USER_ID, paper_id)
                    stats["linked"] += 1
                conn.commit()
                print(f"  [{tag}] PaperID={paper_id} | {title}...")
            except Exception as e:
                conn.rollback()
                stats["errors"] += 1
                print(f"  [ERROR] {title}... :: {e}")

        with conn.cursor() as cur:
            stamp_last_synced(cur, USER_ID)
        conn.commit()

    print()
    print("-" * 60)
    print(f"DONE: {stats}")


if __name__ == "__main__":
    main()
