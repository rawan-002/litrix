"""Mark book-chapter papers with VenueType='Book'.

The authoritative verifier (verify_venue_authoritative.py) flagged papers whose
OpenAlex AND Crossref record agree the DOI resolves to a book / book-chapter
(reason='book-chapter'): encyclopedias, IGI Global / WORLD SCIENTIFIC eBooks,
Springer book chapters, etc. These are neither Journal nor Conference, yet they
sat under 'Journal' and so (a) inflated the journal count and (b) let their
container's Scopus Quartile count toward Q1-Q4 KPIs.

We give them their own type 'Book' so the reporting layer can exclude them from
both the journal split and the quartile KPIs.

Safety: only rows the verifier tagged 'book-chapter' AND that are currently
'Journal' are touched. Rows already 'Conference' are LEFT ALONE - a couple of
book-series proceedings (LNICST, EAI conference volumes) are genuinely
conferences and their Conference label is more correct than 'Book'.

Idempotent. Dry-run by default; pass --commit to write.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from litrix_db import db, setup_utf8_stdout

setup_utf8_stdout()

CHECKPOINT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'venue_verify_checkpoint.jsonl')


def book_pids():
    """PaperIDs the verifier tagged 'book-chapter' and left classified 'Journal'."""
    pids = []
    with open(CHECKPOINT, encoding='utf-8') as f:
        for line in f:
            r = json.loads(line)
            if r.get('reason') == 'book-chapter' and r.get('current') == 'Journal':
                pids.append((r['pid'], (r.get('names') or '')))
    return pids


def main():
    ap = argparse.ArgumentParser(description="Mark book chapters VenueType='Book'")
    ap.add_argument('--commit', action='store_true', help='write changes (default: dry-run)')
    args = ap.parse_args()

    targets = book_pids()
    conn = db()
    cur = conn.cursor()

    print('=' * 70)
    print(f"Book chapters to mark 'Book' (currently 'Journal'): {len(targets)}")
    print('=' * 70)
    for pid, name in targets:
        print(f'  P{pid}: {name[:74]}')

    if not args.commit:
        print('\nDRY-RUN. Re-run with --commit to apply.')
        return

    ids = [pid for pid, _ in targets]
    # Idempotent + guarded: only flip rows still 'Journal'.
    cur.execute(
        'UPDATE "ResearchPaper" SET "VenueType" = %s '
        'WHERE "PaperID" = ANY(%s) AND "VenueType" = %s',
        ['Book', ids, 'Journal'])
    conn.commit()
    print(f'\nCOMMITTED: {cur.rowcount} rows set to Book.')

    cur.execute('SELECT "VenueType", COUNT(*) FROM "ResearchPaper" GROUP BY 1 ORDER BY 2 DESC')
    print('New distribution:', cur.fetchall())


if __name__ == '__main__':
    main()
