import os
import sys
import io
import argparse
from dotenv import load_dotenv
import psycopg2

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
except Exception:
    pass

load_dotenv()


# db() lives in litrix_db.py at the repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from litrix_db import db


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", type=int, help="Wipe one researcher's papers only")
    ap.add_argument("--source", choices=["Scholar", "OpenAlex", "Both", "Manual", "ORCID"],
                    nargs="+", default=["Scholar", "OpenAlex", "Both"])
    ap.add_argument("--confirm", action="store_true", required=False)
    args = ap.parse_args()

    if not args.confirm:
        print("DRY RUN — add --confirm to actually delete\n")

    conn = db()
    cur = conn.cursor()
    print(f"Connected to: {os.getenv('DATABASE_URL', 'LOCAL').split('@')[-1].split('/')[0]}\n")

    if args.user:
        cur.execute(
            'SELECT COUNT(*) FROM "Authors" WHERE "UserID" = %s',
            (args.user,),
        )
        n_links = cur.fetchone()[0]
        print(f"Authors links for UID {args.user}: {n_links}")

        if args.confirm:
            cur.execute('DELETE FROM "Authors" WHERE "UserID" = %s', (args.user,))
            print(f"  deleted {cur.rowcount} Authors links")
            cur.execute('''
                DELETE FROM "ResearchPaper"
                WHERE "PaperID" NOT IN (SELECT DISTINCT "PaperID" FROM "Authors")
            ''')
            print(f"  deleted {cur.rowcount} orphan papers")
            conn.commit()
    else:
        cur.execute(
            'SELECT COUNT(*) FROM "ResearchPaper" WHERE "Source" = ANY(%s)',
            (args.source,),
        )
        n_papers = cur.fetchone()[0]
        print(f"Papers with Source IN {args.source}: {n_papers}")

        if args.confirm:
            cur.execute(
                'DELETE FROM "Authors" WHERE "PaperID" IN '
                '(SELECT "PaperID" FROM "ResearchPaper" WHERE "Source" = ANY(%s))',
                (args.source,),
            )
            print(f"  deleted {cur.rowcount} Authors links")
            cur.execute(
                'DELETE FROM "ResearchPaper" WHERE "Source" = ANY(%s)',
                (args.source,),
            )
            print(f"  deleted {cur.rowcount} papers")
            conn.commit()

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
