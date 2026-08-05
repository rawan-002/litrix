"""Reclassify Wiley book chapters mislabeled as Journal/Conference.

Root cause: verify_venue_authoritative.py only auto-applies a VenueType when
Crossref AND OpenAlex agree. For Wiley chapters like "Wiley Data and
Cybersecurity, 2021" ch3/ch5/ch6/ch7, neither API returned a clean
'book-chapter' verdict (Crossref tagged them 'other' despite resolving the
correct book title; OpenAlex had no record at all), so classify() fell
through to reason='no-evidence' / 'dblp-only' / 'stored-name' and whatever
Journal/Conference label was already stored just stuck.

verify_venue_authoritative.py now carries a permanent fix for this (Rule A /
step 0 in classify(): DOI matching 10.1002/<13-digit ISBN>.ch<N> is Wiley's
own book-chapter DOI convention, unambiguous by construction, so it settles
VenueType='Book' with no API call). That fixes every FUTURE import.

This script is the one-time backfill for papers scraped - and checkpointed
with a stale decision - before that rule existed. It re-scans every paper
against the same regex directly (bypassing the checkpoint, since a cached
'done' entry would otherwise replay the old wrong decision forever) and
patches venue_verify_checkpoint.jsonl so a future verify_venue_authoritative.py
run sees the corrected decision instead of reconsidering it.

Idempotent: only touches rows where VenueType is not already 'Book'.
Safe to rerun: YES.
Dry-run by default.

  python fix_wiley_book_chapter_doi.py             # preview
  python fix_wiley_book_chapter_doi.py --commit     # write + patch checkpoint
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from litrix_db import db, setup_utf8_stdout

setup_utf8_stdout()

# Kept identical to verify_venue_authoritative.py's WILEY_BOOK_CHAPTER_DOI -
# these are separate one-off scripts by project convention, not a shared module.
WILEY_BOOK_CHAPTER_DOI = re.compile(r'^10\.1002/97[0-9]{11}\.ch[0-9]+$', re.I)

CHECKPOINT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'venue_verify_checkpoint.jsonl')


def find_targets(cur):
    cur.execute('''
        SELECT "PaperID", "Title", "DOI", "VenueType"
        FROM "ResearchPaper"
        WHERE "DOI" IS NOT NULL AND "VenueType" IS DISTINCT FROM 'Book'
        ORDER BY "PaperID"
    ''')
    return [(pid, title, doi, vt) for pid, title, doi, vt in cur.fetchall()
            if WILEY_BOOK_CHAPTER_DOI.match(doi.strip())]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--commit', action='store_true', help='write changes (default: dry-run)')
    args = ap.parse_args()

    conn = db()
    cur = conn.cursor()
    targets = find_targets(cur)

    print('=' * 70)
    print(f"Wiley book-chapter DOIs mislabeled (VenueType != 'Book'): {len(targets)}")
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
        ['Book', ids, 'Book'])
    conn.commit()
    print(f'\nCOMMITTED: {cur.rowcount} rows set to Book.')

    with open(CHECKPOINT, 'a', encoding='utf-8') as f:
        for pid, title, doi, vt in targets:
            f.write(json.dumps({
                'pid': pid, 'title': title, 'stored_name': None, 'current': vt,
                'verdict': 'Book', 'review': False,
                'reason': 'doi-book-chapter-pattern',
                'oa': '', 'cr': '', 'names': '', 'dblp': '',
            }, ensure_ascii=False) + '\n')
    print(f'Checkpoint patched: {len(targets)} entries appended.')


if __name__ == '__main__':
    main()
