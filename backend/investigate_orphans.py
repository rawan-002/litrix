"""
Diagnose WHY 489 papers have no citation_id in RawData_Log.

For each orphan paper we want to know:
  1. What Source did it come from? (Scholar / ORCID / Scopus / Manual)
  2. What top-level keys exist in its RawData_Log?
  3. Does it contain an ORCID, OpenAlex author id, or DOI we can match on?
  4. Could we match it via any registered researcher's identifiers?

The output is a single CSV report + summary so we can decide which
matching strategy to add next.

USAGE
-----
    cd backend
    python investigate_orphans.py
"""
import os, sys, csv, json
from collections import Counter

import django
if not os.environ.get('DJANGO_SETTINGS_MODULE'):
    os.environ['DJANGO_SETTINGS_MODULE'] = 'litrix_backend.settings'
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()

from django.db import connection


def main():
    with connection.cursor() as cur:

        # 1. Source distribution for orphan papers
        cur.execute('''
            SELECT COALESCE(rp."Source", '(null)') AS src, COUNT(*)
            FROM "ResearchPaper" rp
            WHERE NOT EXISTS (
                SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID"
            )
            GROUP BY rp."Source"
            ORDER BY 2 DESC
        ''')
        print('Orphans by Source field:')
        for src, n in cur.fetchall():
            print(f'  {src:<20} {n}')
        print()

        # 2. Top-level keys present in RawData_Log of orphans
        cur.execute('''
            SELECT jsonb_object_keys(rp."RawData_Log") AS k, COUNT(*) AS n
            FROM "ResearchPaper" rp
            WHERE rp."RawData_Log" IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID"
              )
            GROUP BY k
            ORDER BY n DESC
        ''')
        print('Top-level keys in orphans RawData_Log:')
        for k, n in cur.fetchall():
            print(f'  {k:<30} {n}')
        print()

        # 3. How many orphans have each identifier we could match on?
        print('Identifier coverage in orphans:')
        cur.execute('''
            SELECT
                COUNT(*) FILTER (WHERE rp."DOI" IS NOT NULL)                                      AS has_doi,
                COUNT(*) FILTER (WHERE rp."RawData_Log" ? 'citation_id')                          AS has_citation_id,
                COUNT(*) FILTER (WHERE rp."RawData_Log" ? 'authorships')                          AS has_authorships,
                COUNT(*) FILTER (WHERE rp."RawData_Log" ? 'orcid_works')                          AS has_orcid_works,
                COUNT(*) FILTER (WHERE rp."RawData_Log" ? 'scopus_eid')                           AS has_scopus_eid,
                COUNT(*) FILTER (WHERE rp."RawData_Log" ? 'authors')                              AS has_authors_str,
                COUNT(*)                                                                          AS total
            FROM "ResearchPaper" rp
            WHERE NOT EXISTS (
                SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID"
            )
        ''')
        r = cur.fetchone()
        labels = ['has_doi','has_citation_id','has_authorships',
                  'has_orcid_works','has_scopus_eid','has_authors_str','total']
        for k, v in zip(labels, r):
            print(f'  {k:<20} {v}')
        print()

        # 4. Sample 5 orphans and dump their RawData_Log keys
        cur.execute('''
            SELECT rp."PaperID", LEFT(rp."Title", 70),
                   rp."Source", rp."DOI",
                   COALESCE(
                       (SELECT jsonb_object_keys(rp."RawData_Log") LIMIT 1),
                       '(none)'
                   ),
                   jsonb_path_query_array(rp."RawData_Log", '$.keyvalue().key') AS keys
            FROM "ResearchPaper" rp
            WHERE NOT EXISTS (
                SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID"
            )
            ORDER BY rp."PaperID"
            LIMIT 5
        ''')
        print('Sample orphans (full key list per paper):')
        for paper_id, title, source, doi, _, keys in cur.fetchall():
            keys_str = keys if isinstance(keys, str) else json.dumps(keys)
            print(f'\n  PaperID={paper_id}  source={source!r}  doi={doi!r}')
            print(f'    title: {title}')
            print(f'    keys : {keys_str}')
        print()

        # 5. Try DOI-based reverse match: any orphan paper whose DOI appears
        #    in another (non-orphan) paper's Authors via co-author network?
        cur.execute('''
            WITH orphan_dois AS (
                SELECT rp."PaperID", LOWER(rp."DOI") AS doi
                FROM "ResearchPaper" rp
                WHERE rp."DOI" IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID"
                  )
            )
            SELECT COUNT(*) FROM orphan_dois
        ''')
        with_doi = cur.fetchone()[0]
        print(f'Orphans with a DOI we could potentially de-duplicate against: {with_doi}')

        # 6. CSV export of all orphans for human review
        csv_path = 'orphan_papers_full.csv'
        cur.execute('''
            SELECT rp."PaperID", rp."Title", rp."PubYear",
                   rp."Source", rp."DOI",
                   rp."RawData_Log"->>'citation_id'  AS citation_id,
                   rp."RawData_Log"->>'authors'       AS authors_text,
                   jsonb_path_query_array(
                       rp."RawData_Log"->'authorships',
                       '$[*].author.display_name'
                   ) AS authorships_names,
                   jsonb_path_query_array(
                       rp."RawData_Log"->'authorships',
                       '$[*].author.orcid'
                   ) AS authorships_orcids
            FROM "ResearchPaper" rp
            WHERE NOT EXISTS (
                SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID"
            )
            ORDER BY rp."PubYear" DESC NULLS LAST, rp."PaperID"
        ''')
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['PaperID','Title','PubYear','Source','DOI',
                        'citation_id','authors_text',
                        'authorships_names','authorships_orcids'])
            for row in cur.fetchall():
                w.writerow([
                    row[0], row[1], row[2], row[3], row[4],
                    row[5] or '',
                    (row[6] or '')[:300],
                    json.dumps(row[7]) if row[7] else '',
                    json.dumps(row[8]) if row[8] else '',
                ])
        print(f'\nFull orphan report saved to: {csv_path}')


if __name__ == '__main__':
    main()
