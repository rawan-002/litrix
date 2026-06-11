"""Read-only health check over the Litrix domain tables.

Safe to run unattended — no writes, no API calls (that's the difference from
verify_attributions.py, which deletes). It mainly guards against a repeat of the
name-collision bug that once cross-contaminated 602 papers (OPERATIONS_LOG.md).
HARD checks exit non-zero so a cron/CI run alerts; WATCH metrics are reported
only, unless --strict or they cross a generous ceiling.

    python tools/integrity_check.py [--strict] [--json] [--max-internal-authors N]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from litrix_db import db, setup_utf8_stdout

setup_utf8_stdout()


def _scalar(cur, sql, params=None):
    cur.execute(sql, params or [])
    return cur.fetchone()[0]


def run_checks(cur, args):
    findings = []

    def add(key, label, count, tier, ok, detail=''):
        findings.append({
            'key': key, 'label': label, 'count': int(count),
            'tier': tier, 'ok': bool(ok), 'detail': detail,
        })

    dup_dois = _scalar(cur, '''
        SELECT COUNT(*) FROM (
            SELECT LOWER("DOI")
            FROM "ResearchPaper"
            WHERE "DOI" IS NOT NULL AND "DOI" <> ''
            GROUP BY LOWER("DOI") HAVING COUNT(*) > 1
        ) g
    ''')
    add('duplicate_dois', 'Duplicate DOIs (same DOI on >1 paper)', dup_dois,
        'HARD', dup_dois == 0)

    # A paper credited to too many of our own researchers is the classic
    # name-collision contamination signature.
    over_attr = _scalar(cur, '''
        SELECT COUNT(*) FROM (
            SELECT a."PaperID"
            FROM "Authors" a
            GROUP BY a."PaperID"
            HAVING COUNT(DISTINCT a."UserID") > %s
        ) g
    ''', [args.max_internal_authors])
    add('over_attributed_papers',
        f'Papers attributed to > {args.max_internal_authors} internal researchers',
        over_attr, 'HARD', over_attr == 0,
        'name-collision contamination signal')

    dangling = _scalar(cur, '''
        SELECT COUNT(*) FROM "Authors" a
        WHERE NOT EXISTS (SELECT 1 FROM "ResearchPaper" rp WHERE rp."PaperID" = a."PaperID")
           OR NOT EXISTS (SELECT 1 FROM "Users" u WHERE u."UserID" = a."UserID")
    ''')
    add('dangling_author_links', 'Authors links to a missing paper/user',
        dangling, 'HARD', dangling == 0)

    total_papers = _scalar(cur, 'SELECT COUNT(*) FROM "ResearchPaper"')
    orphans = _scalar(cur, '''
        SELECT COUNT(*) FROM "ResearchPaper" rp
        WHERE NOT EXISTS (SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID")
    ''')
    orphan_ratio = (orphans / total_papers) if total_papers else 0
    add('orphan_papers',
        f'Orphan papers (no author) — {orphan_ratio:.0%} of {total_papers}',
        orphans, 'WATCH', orphan_ratio <= args.max_orphan_ratio,
        f'ceiling {args.max_orphan_ratio:.0%}')

    no_papers = _scalar(cur, '''
        SELECT COUNT(*) FROM "Users" u
        WHERE u."UserType" = 'Researcher'
          AND NOT EXISTS (SELECT 1 FROM "Authors" a WHERE a."UserID" = u."UserID")
    ''')
    add('researchers_no_papers', 'Researchers with 0 attributed papers',
        no_papers, 'WATCH', no_papers <= args.max_zero_paper_researchers,
        f'ceiling {args.max_zero_paper_researchers}')

    # Not corruption — just signals the affiliation filter is going stale.
    unverified = _scalar(cur, '''
        SELECT COUNT(*) FROM "ResearchPaper" rp
        WHERE rp."AffiliationVerified" IS NULL
          AND rp."DOI" IS NOT NULL AND rp."DOI" <> ''
          AND EXISTS (SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID")
    ''')
    add('affiliation_backlog',
        'Attributed papers with a DOI awaiting affiliation verification',
        unverified, 'WATCH', True, 'informational')

    return findings


def main():
    p = argparse.ArgumentParser(description='Litrix read-only data-integrity check')
    p.add_argument('--strict', action='store_true',
                   help='WATCH metrics over their ceiling fail the run too')
    p.add_argument('--json', action='store_true', help='machine-readable output')
    p.add_argument('--max-internal-authors', type=int, default=10)
    p.add_argument('--max-orphan-ratio', type=float, default=0.60)
    p.add_argument('--max-zero-paper-researchers', type=int, default=45)
    args = p.parse_args()

    try:
        conn = db()
    except psycopg2.Error as e:
        print(f'ERROR: DB connection failed: {e}', file=sys.stderr)
        sys.exit(2)

    try:
        with conn.cursor() as cur:
            findings = run_checks(cur, args)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(findings, ensure_ascii=False, indent=2))

    hard_fail = [f for f in findings if f['tier'] == 'HARD' and not f['ok']]
    watch_fail = [f for f in findings if f['tier'] == 'WATCH' and not f['ok']]

    if not args.json:
        print()
        print('=' * 72)
        print(' LITRIX DATA-INTEGRITY CHECK'.center(72))
        print('=' * 72)
        for tier in ('HARD', 'WATCH'):
            print(f'\n  [{tier}]')
            for f in [x for x in findings if x['tier'] == tier]:
                mark = 'OK  ' if f['ok'] else ('FAIL' if tier == 'HARD' else 'WARN')
                extra = f'   ({f["detail"]})' if f['detail'] else ''
                print(f'    {mark}  {f["count"]:>6}  {f["label"]}{extra}')
        print('\n' + '-' * 72)

    failed = bool(hard_fail) or (args.strict and bool(watch_fail))
    if not args.json:
        if failed:
            n = len(hard_fail) + (len(watch_fail) if args.strict else 0)
            print(f'  RESULT: ALERT — {n} check(s) need attention.')
        else:
            warn = f' ({len(watch_fail)} watch warning(s))' if watch_fail else ''
            print(f'  RESULT: healthy{warn}.')
        print('=' * 72)

    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
