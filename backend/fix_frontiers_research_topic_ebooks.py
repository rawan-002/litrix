"""Reclassify Frontiers "Research Topic" ebook compilations back to Journal.

Root cause: Frontiers compiles a themed Research Topic - a curated set of
ALREADY-published articles from that same journal, each with its own regular
article DOI - into a downloadable PDF/EPUB and registers that compilation
itself with Crossref as type='edited-book' (DOI shaped like an ISBN, e.g.
10.3389/978-2-8325-6894-1). That's accurate Crossref metadata for the
compilation, but wrong for our stats: the individual articles are already
counted as Journal papers under their own authors, so also counting the
compilation as a 'Book' both double-counts content and turns an editorial/
curation credit into book authorship.

Found via researcher Najib Ben Aoun's "Neuro-detection..." Research Topic
(PaperID 5526): Crossref tagged it edited-book, OpenAlex tagged it book/
journal, so verify_venue_authoritative.py flagged it review=True (reason
'book-chapter' at the time), and the older mark_book_venues.py auto-applied
'Book' to it since it was still 'Journal' when reviewed.

venue_classifiers now carries a permanent fix (publishers.py's
FRONTIERS_RESEARCH_TOPIC_EBOOK rule: DOI matching 10.3389/97[89]<digits>
overrides straight to Journal, no API call). That fixes every FUTURE import.

This script is the one-time backfill for papers already in the DB (and
possibly already checkpointed with the old wrong decision) before that rule
existed. Idempotent: only touches rows where VenueType is not already
'Journal'. Dry-run by default.

  python fix_frontiers_research_topic_ebooks.py             # preview
  python fix_frontiers_research_topic_ebooks.py --commit     # write + patch checkpoint
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from litrix_db import db, setup_utf8_stdout
from venue_classifiers import JOURNAL, classify_from_doi

setup_utf8_stdout()

CHECKPOINT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'venue_verify_checkpoint.jsonl')


def find_targets(cur):
    cur.execute('''
        SELECT "PaperID", "Title", "DOI", "VenueType"
        FROM "ResearchPaper"
        WHERE "DOI" IS NOT NULL AND "VenueType" IS DISTINCT FROM 'Journal'
        ORDER BY "PaperID"
    ''')
    return [(pid, title, doi, vt) for pid, title, doi, vt in cur.fetchall()
            if classify_from_doi(doi) == JOURNAL]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--commit', action='store_true', help='write changes (default: dry-run)')
    args = ap.parse_args()

    conn = db()
    cur = conn.cursor()
    targets = find_targets(cur)

    print('=' * 70)
    print(f"Frontiers Research Topic ebooks mislabeled (VenueType != 'Journal'): {len(targets)}")
    print('=' * 70)
    for pid, title, doi, vt in targets:
        print(f'  P{pid} [{vt:<10}] {doi}  {(title or "")[:70]}')

    if not args.commit:
        print('\nDRY-RUN. Re-run with --commit to apply.')
        return

    if not targets:
        print('\nNothing to do.')
        return

    ids = [pid for pid, _, _, _ in targets]
    cur.execute(
        'UPDATE "ResearchPaper" SET "VenueType" = %s '
        'WHERE "PaperID" = ANY(%s) AND "VenueType" IS DISTINCT FROM %s',
        ['Journal', ids, 'Journal'])
    conn.commit()
    print(f'\nCOMMITTED: {cur.rowcount} rows set to Journal.')

    with open(CHECKPOINT, 'a', encoding='utf-8') as f:
        for pid, title, doi, vt in targets:
            f.write(json.dumps({
                'pid': pid, 'title': title, 'stored_name': None, 'current': vt,
                'verdict': 'Journal', 'review': False,
                'reason': 'doi-pattern',
                'oa': '', 'cr': '', 'names': '', 'dblp': '',
            }, ensure_ascii=False) + '\n')
    print(f'Checkpoint patched: {len(targets)} entries appended.')


if __name__ == '__main__':
    main()
