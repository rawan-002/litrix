import os
import sys
import io
import argparse
from collections import defaultdict
from dotenv import load_dotenv
import psycopg2

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
except Exception:
    pass

load_dotenv()


def db():
    return psycopg2.connect(
        os.getenv("DATABASE_URL") or
        f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
        f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep-duplicates", action="store_true")
    args = ap.parse_args()

    conn = db()
    cur = conn.cursor()
    print(f"Connected to: {os.getenv('DATABASE_URL', 'LOCAL').split('@')[-1].split('/')[0]}\n")

    cur.execute('''
        SELECT "JournalID", "JournalName", "ISSN_Print", "NormalizedName"
        FROM "Journals"
        WHERE "NormalizedName" IS NOT NULL
        ORDER BY "NormalizedName", "JournalID"
    ''')
    rows = cur.fetchall()

    groups = defaultdict(list)
    for jid, name, issn, norm in rows:
        groups[norm].append({"jid": jid, "name": name, "issn": issn})

    dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
    print(f"Total groups: {len(groups)}")
    print(f"Duplicate groups: {len(dup_groups)}\n")

    if not dup_groups:
        return

    n_papers = n_deleted = n_ambiguous = 0

    for norm, members in dup_groups.items():
        with_issn = [m for m in members if m["issn"]]
        if len(with_issn) > 1:
            issns = set(m["issn"] for m in with_issn)
            if len(issns) > 1:
                n_ambiguous += 1
                continue
            canonical = with_issn[0]
        elif len(with_issn) == 1:
            canonical = with_issn[0]
        else:
            canonical = min(members, key=lambda m: m["jid"])

        duplicates = [m for m in members if m["jid"] != canonical["jid"]]
        dup_jids = [m["jid"] for m in duplicates]

        cur.execute(
            'SELECT COUNT(*) FROM "ResearchPaper" WHERE "JournalID" = ANY(%s)',
            (dup_jids,),
        )
        paper_count = cur.fetchone()[0]

        print(f"  '{norm}' canonical={canonical['jid']} dups={dup_jids} papers={paper_count}")

        if args.dry_run:
            n_papers += paper_count
            n_deleted += len(dup_jids)
            continue

        cur.execute(
            'UPDATE "ResearchPaper" SET "JournalID" = %s WHERE "JournalID" = ANY(%s)',
            (canonical["jid"], dup_jids),
        )
        n_papers += cur.rowcount

        if not canonical["issn"]:
            for m in duplicates:
                if m["issn"]:
                    cur.execute(
                        'UPDATE "Journals" SET "ISSN_Print" = %s '
                        'WHERE "JournalID" = %s AND "ISSN_Print" IS NULL',
                        (m["issn"], canonical["jid"]),
                    )
                    break

        if not args.keep_duplicates:
            cur.execute(
                'DELETE FROM "Journals" WHERE "JournalID" = ANY(%s)',
                (dup_jids,),
            )
            n_deleted += cur.rowcount

        conn.commit()

    print(f"\n{'[DRY RUN] would: ' if args.dry_run else 'Done: '}"
          f"re-point {n_papers} papers, delete {n_deleted} rows, "
          f"skip {n_ambiguous} ambiguous")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
