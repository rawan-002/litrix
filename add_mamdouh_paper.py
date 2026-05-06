"""Manual insert: Mamdouh Hassan's paper from EBSCO (not yet on OpenAlex).

Why manual:
  - The paper is too new (Conference Proceeding 2026, Advances in
    Consumer Research). OpenAlex hasn't indexed it. We have full
    metadata from EBSCO so we insert it directly.

What we have:
  Title:      AI-Driven Financial Crime Analytics: ...
  Authors:    Hassan, Mumdouh Mirghani Mohamed; et al.
  Journal:    Advances in Consumer Research, ISSN 0098-9258
  Type:       Conference Proceeding
  Year:       2026
  Vol/Issue:  Vol 3, Issue 1, p893
  DOI:        none provided

CitationsByYear stays NULL — OpenAlex will populate it on a later
re-scrape if/when it gets indexed.
"""
import os
import sys
import argparse
import psycopg2
from psycopg2.extras import Json
from dotenv import load_dotenv

load_dotenv()

PAPER = {
    "title":     "AI-Driven Financial Crime Analytics: Strengthening Fraud Detection via Graph Intelligence and Blockchain Trace Forensics",
    "doi":       None,
    "pub_year":  2026,
    "source":    "Manual_EBSCO",
    "indexing":  "Other",  # not Scopus/ISI as far as we know
}
JOURNAL = {
    "name":       "Advances in Consumer Research",
    "issn_print": "0098-9258",
    "venue_type": "Conference",  # EBSCO classified it as Conference Proceeding
}
USER_ID = 99
RAW_NAME = "Hassan, Mumdouh Mirghani Mohamed"


def db_connect():
    url = os.getenv("DATABASE_URL")
    if url:
        return psycopg2.connect(url)
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "LitrixDB"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
    )


def find_or_create_journal(cur):
    """Match by ISSN first, then by name. Insert if neither matches."""
    cur.execute(
        'SELECT "JournalID", "JournalName" FROM "Journals" WHERE "ISSN_Print" = %s',
        (JOURNAL["issn_print"],),
    )
    row = cur.fetchone()
    if row:
        print(f"  ✓ existing journal by ISSN: JournalID={row[0]}  {row[1]}")
        return row[0]

    cur.execute(
        'SELECT "JournalID", "JournalName" FROM "Journals" WHERE LOWER("JournalName") = LOWER(%s)',
        (JOURNAL["name"],),
    )
    row = cur.fetchone()
    if row:
        print(f"  ✓ existing journal by name: JournalID={row[0]}  {row[1]}")
        # Backfill ISSN if missing
        cur.execute(
            'UPDATE "Journals" SET "ISSN_Print" = COALESCE("ISSN_Print", %s) WHERE "JournalID" = %s',
            (JOURNAL["issn_print"], row[0]),
        )
        return row[0]

    cur.execute(
        '''INSERT INTO "Journals" ("JournalName", "ISSN_Print", "VenueType")
           VALUES (%s, %s, %s) RETURNING "JournalID"''',
        (JOURNAL["name"], JOURNAL["issn_print"], JOURNAL["venue_type"]),
    )
    new_id = cur.fetchone()[0]
    print(f"  + inserted new journal: JournalID={new_id}  {JOURNAL['name']}")
    return new_id


def find_or_create_paper(cur, journal_id):
    """Match by title (case-insensitive). Insert if not found."""
    cur.execute(
        'SELECT "PaperID" FROM "ResearchPaper" WHERE LOWER("Title") = LOWER(%s) LIMIT 1',
        (PAPER["title"],),
    )
    row = cur.fetchone()
    if row:
        print(f"  ✓ existing paper: PaperID={row[0]}")
        # Backfill missing fields
        cur.execute("""
            UPDATE "ResearchPaper"
            SET "JournalID" = COALESCE("JournalID", %s),
                "PubYear"   = COALESCE("PubYear", %s),
                "Indexing"  = COALESCE("Indexing", %s),
                "Source"    = COALESCE("Source", %s)
            WHERE "PaperID" = %s
        """, (journal_id, PAPER["pub_year"], PAPER["indexing"], PAPER["source"], row[0]))
        return row[0]

    cur.execute("""
        INSERT INTO "ResearchPaper" (
            "Title", "DOI", "PubYear", "Source", "JournalID", "Indexing", "RawData_Log"
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING "PaperID"
    """, (
        PAPER["title"], PAPER["doi"], PAPER["pub_year"], PAPER["source"],
        journal_id, PAPER["indexing"],
        Json({
            "source_db": "EBSCO",
            "issn":      JOURNAL["issn_print"],
            "volume":    "3",
            "issue":     "1",
            "page":      "893",
            "type":      "Conference Proceeding",
        }),
    ))
    new_id = cur.fetchone()[0]
    print(f"  + inserted new paper: PaperID={new_id}")
    return new_id


def link_author(cur, paper_id):
    """Link UserID to PaperID via Authors with verified flag."""
    cur.execute("""
        INSERT INTO "Authors" (
            "UserID", "PaperID", "AuthorOrder",
            "IsCorrespondingAuthor", "MappingConfidence", "MappingCriteria",
            "AuthorNameRaw", "Is_Verified"
        )
        VALUES (%s, %s, 1, TRUE, 1.0, 'manual_ebsco', %s, TRUE)
        ON CONFLICT ("UserID", "PaperID") DO UPDATE
        SET "IsCorrespondingAuthor" = TRUE,
            "Is_Verified" = TRUE,
            "MappingCriteria" = EXCLUDED."MappingCriteria"
    """, (USER_ID, paper_id, RAW_NAME))
    print(f"  ✓ linked UserID={USER_ID} → PaperID={paper_id}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print("=" * 60)
    print(f"add_mamdouh_paper — UserID={USER_ID}")
    print(f"Mode: {'DRY-RUN' if args.dry_run else 'WRITE'}")
    print("=" * 60)

    conn = db_connect()
    cur = conn.cursor()

    # Verify user
    cur.execute('SELECT "FullName_Ar", "Email" FROM "Users" WHERE "UserID" = %s', (USER_ID,))
    row = cur.fetchone()
    if not row:
        print(f"[!] No user UserID={USER_ID}")
        sys.exit(1)
    print(f"  Researcher: {row[0]}  ({row[1]})")

    try:
        print("\n[1] Journal:")
        journal_id = find_or_create_journal(cur)

        print("\n[2] Paper:")
        paper_id = find_or_create_paper(cur, journal_id)

        print("\n[3] Authors link:")
        link_author(cur, paper_id)

        if args.dry_run:
            conn.rollback()
            print("\n[DRY-RUN] rolled back — no changes saved.")
        else:
            conn.commit()
            print("\n✓ DONE — committed.")
    except Exception as e:
        conn.rollback()
        print(f"\n[!] Error — rolled back: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
