"""Apply the FINAL venue decisions for the 60 papers that were VenueType IS NULL.

Verdicts are explicit (not recomputed) so the write is fast, deterministic and
auditable: 46 came from the deterministic classifier (classify_null_venues.py
dry-run: stored-name / doi-agree / stored-serial), 13 more were resolved by a
manual web check (arXiv/Cureus/Nature/IJCSE/... venue lookups). One row is a
patent (not a research paper) and is deliberately LEFT NULL for a human to
decide whether it belongs to the researcher at all.

Introduces a 'Preprint' VenueType for the two non-peer-reviewed rows
(preprints.org, arXiv). NOTE: the reporting gate must also exclude 'Preprint'
from journal counts / Q-KPIs (same as 'Book') -- that code change ships with the
pending deploy; this script only sets the data.

Idempotent + guarded: UPDATE ... WHERE "PaperID"=%s AND "VenueType" IS NULL.
Dry-run by default; pass --commit to write.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from litrix_db import db, setup_utf8_stdout

setup_utf8_stdout()

JOURNAL = [
    # 36 from the deterministic classifier (high-confidence)
    7540, 7541, 7542, 7543, 7544, 7546, 7547, 7548, 7550, 7554,
    7560, 7561, 7562, 7563, 7564, 7565, 7566, 7567, 7568, 7569,
    7571, 7573, 7575, 7576, 7577, 7580, 7581, 7582, 7583, 7586,
    7587, 7589, 7590, 7594, 7595, 7596,
    # 11 resolved by web check (venue is a known journal)
    7545, 7549, 7553, 7557, 7559, 7574, 7579, 7584, 7585, 7588, 7597,
]
CONFERENCE = [7551, 7552, 7555, 7556, 7558, 7570, 7578, 7591, 7592, 7593]
PREPRINT = [7539, 7572]
# 7598 = GB Patent -> intentionally left NULL for manual review.


def main():
    ap = argparse.ArgumentParser(description='Apply final NULL-venue decisions')
    ap.add_argument('--commit', action='store_true', help='write changes (default: dry-run)')
    args = ap.parse_args()

    plan = ([(p, 'Journal') for p in JOURNAL]
            + [(p, 'Conference') for p in CONFERENCE]
            + [(p, 'Preprint') for p in PREPRINT])

    conn = db()
    cur = conn.cursor()
    print('=' * 60)
    print('Journal: %d   Conference: %d   Preprint: %d   (P7598 left NULL)'
          % (len(JOURNAL), len(CONFERENCE), len(PREPRINT)))
    print('Total to set: %d' % len(plan))
    print('=' * 60)

    if not args.commit:
        print('DRY-RUN. Re-run with --commit to apply.')
        return

    n = 0
    for pid, venue in plan:
        cur.execute(
            'UPDATE "ResearchPaper" SET "VenueType"=%s '
            'WHERE "PaperID"=%s AND "VenueType" IS NULL',
            [venue, pid])
        n += cur.rowcount
    conn.commit()
    print('COMMITTED: %d rows updated (rows already non-NULL are skipped).' % n)

    cur.execute('SELECT "VenueType", COUNT(*) FROM "ResearchPaper" GROUP BY 1 ORDER BY 2 DESC')
    print('New distribution:', cur.fetchall())
    cur.execute('SELECT COUNT(*) FROM "ResearchPaper" WHERE "VenueType" IS NULL')
    print('Remaining NULL:', cur.fetchone()[0], '(expected 1: P7598 patent)')


if __name__ == '__main__':
    main()
