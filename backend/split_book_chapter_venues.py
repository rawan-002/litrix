"""Split the single VenueType='Book' bucket into 'Book' (standalone volume)
vs 'BookChapter' (a chapter within one), now that the frontend distinguishes
them (Books tab, dashboard cards, profile sections).

Every current 'Book' row was put there by one of two paths, both of which
already recorded enough evidence to make the split mechanical - no new API
calls needed:

  1. venue_classifiers.classify_from_doi(doi) fires (currently just Wiley's
     10.1002/<ISBN>.ch<N> chapter-DOI rule) -> BookChapter, always. Re-run
     live against each paper's own DOI rather than trusting the checkpoint's
     old 'reason' string, so this stays correct as venue_classifiers grows
     new rules.
  2. venue_verify_authoritative.py's Crossref/OpenAlex agreement path ->
     checkpointed 'oa'/'cr' fields carry the raw work type ('book-chapter'
     vs 'book'/'monograph'/'edited-book'). 'chapter' in either -> BookChapter,
     else -> stays Book.

Rows with no checkpoint entry at all are left untouched and printed for
manual look - guessing isn't worth a silent misclassification here.

Idempotent: only ever moves rows OUT of 'Book' into 'BookChapter'; a
standalone Book never gets touched twice. Dry-run by default.

  python split_book_chapter_venues.py            # preview
  python split_book_chapter_venues.py --commit    # write
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from litrix_db import db, setup_utf8_stdout
from venue_classifiers import BOOK_CHAPTER, classify_from_doi

setup_utf8_stdout()

CHECKPOINT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'venue_verify_checkpoint.jsonl')


def load_checkpoint():
    done = {}
    if os.path.exists(CHECKPOINT):
        with open(CHECKPOINT, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    done[r['pid']] = r
    return done


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--commit', action='store_true', help='write changes (default: dry-run)')
    args = ap.parse_args()

    conn = db()
    cur = conn.cursor()
    cur.execute('SELECT "PaperID", "Title", "DOI" FROM "ResearchPaper" WHERE "VenueType" = %s',
                ['Book'])
    rows = cur.fetchall()
    checkpoint = load_checkpoint()

    to_chapter, stays_book, unresolved = [], [], []
    for pid, title, doi in rows:
        if classify_from_doi(doi) == BOOK_CHAPTER:
            to_chapter.append((pid, title, 'doi-pattern'))
            continue
        r = checkpoint.get(pid)
        if not r:
            unresolved.append((pid, title))
            continue
        oa, cr = (r.get('oa') or ''), (r.get('cr') or '')
        if 'chapter' in oa or 'chapter' in cr:
            to_chapter.append((pid, title, 'checkpoint-oa-cr'))
        else:
            stays_book.append((pid, title))

    print('=' * 70)
    print(f"Book rows: {len(rows)}  |  -> BookChapter: {len(to_chapter)}  "
          f"|  stays Book: {len(stays_book)}  |  unresolved: {len(unresolved)}")
    print('=' * 70)
    for pid, title, src in to_chapter:
        print(f'  P{pid} [{src:<16}] {(title or "")[:70]}')
    if unresolved:
        print('\nUnresolved (no checkpoint entry - left as Book, needs a manual look):')
        for pid, title in unresolved:
            print(f'  P{pid} {(title or "")[:70]}')

    if not args.commit:
        print('\nDRY-RUN. Re-run with --commit to apply.')
        return

    if not to_chapter:
        print('\nNothing to do.')
        return

    ids = [pid for pid, _, _ in to_chapter]
    cur.execute(
        'UPDATE "ResearchPaper" SET "VenueType" = %s '
        'WHERE "PaperID" = ANY(%s) AND "VenueType" = %s',
        ['BookChapter', ids, 'Book'])
    conn.commit()
    print(f'\nCOMMITTED: {cur.rowcount} rows set to BookChapter.')


if __name__ == '__main__':
    main()
