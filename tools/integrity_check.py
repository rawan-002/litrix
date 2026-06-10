"""
============================================================================
LITRIX DATA-INTEGRITY CHECK  (read-only, safe to schedule)
============================================================================
A pure-SQL health check over the domain tables. It NEVER writes — unlike
`verify_attributions.py` (which deletes wrong attributions and calls SerpAPI),
this is safe to run unattended every day.

It exists because name-based matching once cross-contaminated 602 papers
between researchers with similar names (see OPERATIONS_LOG.md). The point is
to catch a regression of that class — or any structural corruption — within a
day, instead of letting it accumulate silently.

Two tiers of findings:
  * HARD  -> invariants that should essentially never hold. ANY hit makes the
            process exit non-zero, so a CI / cron run FAILS and notifies.
  * WATCH -> health metrics that drift with the data. Reported every run;
            they only fail the run with --strict, or when they blow past a
            generous ceiling (catching an explosion, not normal growth).

USAGE
-----
    python tools/integrity_check.py                 # report + hard-fail gate
    python tools/integrity_check.py --strict        # watch metrics fail too
    python tools/integrity_check.py --json          # machine-readable output
    # tune the hard gates:
    python tools/integrity_check.py --max-internal-authors 12

Reads DATABASE_URL from the environment (or .env locally), same as the other
pipeline scripts. Empty -> local Postgres via DB_* vars.
============================================================================
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

import psycopg2

# UTF-8 stdout so Arabic titles / box-drawing don't crash on Windows consoles
# (matches the repo's other scripts).
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
except Exception:
    pass

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


def db():
    url = os.getenv('DATABASE_URL')
    keepalive = dict(keepalives=1, keepalives_idle=30,
                     keepalives_interval=10, keepalives_count=5)
    if url:
        return psycopg2.connect(url, connect_timeout=20, **keepalive)
    return psycopg2.connect(
        host=os.getenv('DB_HOST', 'localhost'),
        port=os.getenv('DB_PORT', '5432'),
        dbname=os.getenv('DB_NAME', 'LitrixDB'),
        user=os.getenv('DB_USER', 'postgres'),
        password=os.getenv('DB_PASSWORD', ''),
        connect_timeout=20, **keepalive,
    )


def _scalar(cur, sql, params=None):
    cur.execute(sql, params or [])
    return cur.fetchone()[0]


def run_checks(cur, args):
    """Returns a list of finding dicts."""
    findings = []

    def add(key, label, count, tier, ok, detail=''):
        findings.append({
            'key': key, 'label': label, 'count': int(count),
            'tier': tier, 'ok': bool(ok), 'detail': detail,
        })

    # ----- HARD invariants ------------------------------------------------

    # 1. Duplicate DOIs: the same DOI must map to exactly one paper. A repeat
    #    means a paper was ingested twice (dedup failure) and will double-count.
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

    # 2. Over-attribution: a paper credited to an implausible number of OUR
    #    researchers is the classic name-collision contamination signature.
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

    # 3. Authors rows pointing at a non-existent paper or user (referential rot).
    dangling = _scalar(cur, '''
        SELECT COUNT(*) FROM "Authors" a
        WHERE NOT EXISTS (SELECT 1 FROM "ResearchPaper" rp WHERE rp."PaperID" = a."PaperID")
           OR NOT EXISTS (SELECT 1 FROM "Users" u WHERE u."UserID" = a."UserID")
    ''')
    add('dangling_author_links', 'Authors links to a missing paper/user',
        dangling, 'HARD', dangling == 0)

    # ----- WATCH metrics --------------------------------------------------

    # 4. Orphan papers: no Litrix author attached. They never surface on the
    #    dashboard, but a sudden jump means an import attributed nothing.
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

    # 5. Researchers with zero attributed papers (sync gaps).
    no_papers = _scalar(cur, '''
        SELECT COUNT(*) FROM "Users" u
        WHERE u."UserType" = 'Researcher'
          AND NOT EXISTS (SELECT 1 FROM "Authors" a WHERE a."UserID" = u."UserID")
    ''')
    add('researchers_no_papers', 'Researchers with 0 attributed papers',
        no_papers, 'WATCH', no_papers <= args.max_zero_paper_researchers,
        f'ceiling {args.max_zero_paper_researchers}')

    # 6. Verifier backlog: papers with a DOI, attributed, still unverified.
    #    Not a corruption — just tells you the affiliation filter is stale.
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
    p.add_argument('--max-internal-authors', type=int, default=10,
                   help='hard-fail if any paper exceeds this many internal authors')
    p.add_argument('--max-orphan-ratio', type=float, default=0.60,
                   help='watch ceiling for orphan-paper share (0-1)')
    p.add_argument('--max-zero-paper-researchers', type=int, default=45,
                   help='watch ceiling for researchers with no papers')
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

    # Exit code: non-zero on any HARD failure, or any WATCH breach under --strict.
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
