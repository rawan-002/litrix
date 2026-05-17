"""
Fix orphan papers — link existing papers to their researcher.

WHAT IT DOES
------------
For every ResearchPaper that has no row in Authors, look at its
RawData_Log->'citation_id' (the Scholar profile it was scraped from) and
match it to Users.Scholar_ID. If found, INSERT the missing Authors row.

This is the minimal, focused fix for the symptom "I see papers but no
researcher attached."

USAGE
-----
    python manage.py shell < fix_orphan_papers.py
    OR
    python fix_orphan_papers.py          (if Django settings are exported)

The script is safe to re-run — it only INSERTs links that don't exist yet.

DRY RUN
-------
Set DRY_RUN = True below to see the count without writing.
"""
import os
import sys
import django

# Bootstrap Django when run as a standalone script
if not os.environ.get('DJANGO_SETTINGS_MODULE'):
    os.environ['DJANGO_SETTINGS_MODULE'] = 'litrix_backend.settings'

# Make sure the backend root is on the path (handles `python fix_orphan_papers.py`)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()

from django.db import connection, transaction


DRY_RUN = False   # set to True to preview without writing


def main():
    with transaction.atomic():
        with connection.cursor() as cur:

            # 1) How many orphan papers do we have right now?
            cur.execute('''
                SELECT COUNT(*)
                FROM "ResearchPaper" rp
                WHERE NOT EXISTS (
                    SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID"
                )
            ''')
            total_orphans = cur.fetchone()[0]
            print(f'Orphan papers (no Authors row): {total_orphans}')

            if total_orphans == 0:
                print('Nothing to do.')
                return

            # 2) Show a few examples so you can sanity-check before writing.
            cur.execute('''
                SELECT rp."PaperID",
                       LEFT(rp."Title", 80),
                       rp."RawData_Log"->>'citation_id' AS scholar_id_in_log
                FROM "ResearchPaper" rp
                WHERE NOT EXISTS (
                    SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID"
                )
                ORDER BY rp."ScrapedAt" DESC NULLS LAST
                LIMIT 5
            ''')
            print('\nSample of orphan papers:')
            for row in cur.fetchall():
                print(f'  PaperID={row[0]:>6}  scholar={row[2] or "—":<20}  {row[1]}')

            # 3) Link them — match citation_id in RawData_Log to Users.Scholar_ID.
            cur.execute('''
                INSERT INTO "Authors" (
                    "UserID", "PaperID", "AuthorOrder",
                    "IsCorrespondingAuthor",
                    "MappingConfidence", "MappingCriteria",
                    "AuthorNameRaw", "Is_Verified"
                )
                SELECT
                    u."UserID",
                    rp."PaperID",
                    NULL,
                    FALSE,
                    1.0,
                    'scholar_id_backfill',
                    rp."RawData_Log"->>'authors',
                    TRUE
                FROM "ResearchPaper" rp
                JOIN "Users" u
                  ON u."Scholar_ID" = rp."RawData_Log"->>'citation_id'
                WHERE u."Scholar_ID" IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID"
                  )
                ON CONFLICT ("UserID", "PaperID") DO NOTHING
            ''')
            linked = cur.rowcount
            print(f'\nLinked: {linked}')

            # 4) What's left? These are papers whose citation_id either
            #    (a) is missing from RawData_Log, or
            #    (b) doesn't match any registered Users.Scholar_ID.
            cur.execute('''
                SELECT COUNT(*)
                FROM "ResearchPaper" rp
                WHERE NOT EXISTS (
                    SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID"
                )
            ''')
            still_orphaned = cur.fetchone()[0]
            print(f'Still orphan after pass 1: {still_orphaned}')

            if still_orphaned > 0:
                # Show why — useful debugging info.
                cur.execute('''
                    SELECT
                        COUNT(*) FILTER (
                            WHERE rp."RawData_Log"->>'citation_id' IS NULL
                        ) AS no_citation_id,
                        COUNT(*) FILTER (
                            WHERE rp."RawData_Log"->>'citation_id' IS NOT NULL
                              AND NOT EXISTS (
                                  SELECT 1 FROM "Users" u
                                  WHERE u."Scholar_ID"
                                      = rp."RawData_Log"->>'citation_id'
                              )
                        ) AS unknown_scholar
                    FROM "ResearchPaper" rp
                    WHERE NOT EXISTS (
                        SELECT 1 FROM "Authors" a
                        WHERE a."PaperID" = rp."PaperID"
                    )
                ''')
                no_cid, unknown = cur.fetchone()
                print(f'  · no citation_id in RawData_Log : {no_cid}')
                print(f'  · citation_id present but no user match: {unknown}')

            if DRY_RUN:
                transaction.set_rollback(True)
                print('\nDRY_RUN = True  →  rolled back. Nothing was written.')
            else:
                print('\nDone. Changes committed.')


if __name__ == '__main__':
    main()
