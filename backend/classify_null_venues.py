"""Classify ONLY the papers that still have VenueType IS NULL.

Targeted, safe companion to verify_venue_authoritative.py: running that full
verifier with --commit would re-evaluate every paper and could flip the 76 book
chapters we set to 'Book' back to 'Journal' (their stored verdict is not 'Book').
This script instead touches ONLY currently-NULL rows, so nothing already
classified can change.

For each NULL paper we reuse the authoritative classify() (name authority ->
OpenAlex/Crossref by DOI -> DBLP tiebreak; all free APIs, no SerpAPI):
  - high-confidence Journal/Conference (review=False)  -> set it
  - reason == 'book-chapter'                           -> set 'Book'
  - no-evidence / anything ambiguous                   -> LEAVE NULL
    (NULL is journal-eligible in the reporting layer -- a safe honest default).

Idempotent + guarded: UPDATE ... WHERE "PaperID"=%s AND "VenueType" IS NULL.
Dry-run by default; pass --commit to write.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from litrix_db import db, setup_utf8_stdout
from verify_venue_authoritative import classify, load_checkpoint

setup_utf8_stdout()


def main():
    ap = argparse.ArgumentParser(description='Classify only NULL-VenueType papers')
    ap.add_argument('--commit', action='store_true', help='write changes (default: dry-run)')
    ap.add_argument('--dblp-sleep', type=float, default=1.5)
    args = ap.parse_args()

    conn = db()
    cur = conn.cursor()
    cur.execute('''
        SELECT rp."PaperID", rp."Title", rp."DOI",
               COALESCE(j."JournalName", rp."RawData_Log"->>'publication') AS vname
        FROM "ResearchPaper" rp
        LEFT JOIN "Journals" j ON j."JournalID" = rp."JournalID"
        WHERE rp."VenueType" IS NULL
        ORDER BY rp."PaperID"
    ''')
    rows = cur.fetchall()
    done = load_checkpoint()
    print('NULL-VenueType papers: %d  | cached in checkpoint: %d'
          % (len(rows), sum(1 for (pid, *_ ) in rows if pid in done)))

    plan = []  # (pid, new_venue, reason)
    for pid, title, doi, vname in rows:
        r = done.get(pid) or classify(title, doi, vname, args.dblp_sleep)
        if r.get('reason') == 'book-chapter':
            plan.append((pid, 'Book', 'book-chapter'))
        elif not r.get('review') and r.get('verdict'):
            plan.append((pid, r['verdict'], r.get('reason')))
        # else: no-evidence / ambiguous -> leave NULL

    from collections import Counter
    by_type = Counter(v for _, v, _ in plan)
    print('=' * 68)
    print('Proposed: %d of %d NULL papers get a type  ->  %s'
          % (len(plan), len(rows), dict(by_type)))
    print('Leaving NULL (no evidence): %d' % (len(rows) - len(plan)))
    print('=' * 68)
    for pid, v, reason in plan:
        print('  P%-6s -> %-11s (%s)' % (pid, v, reason))

    if not args.commit:
        print('\nDRY-RUN. Re-run with --commit to apply.')
        return

    for pid, v, _ in plan:
        cur.execute(
            'UPDATE "ResearchPaper" SET "VenueType"=%s '
            'WHERE "PaperID"=%s AND "VenueType" IS NULL',
            [v, pid])
    conn.commit()
    print('\nCOMMITTED: %d rows classified.' % len(plan))
    cur.execute('SELECT "VenueType", COUNT(*) FROM "ResearchPaper" GROUP BY 1 ORDER BY 2 DESC')
    print('New distribution:', cur.fetchall())


if __name__ == '__main__':
    main()
