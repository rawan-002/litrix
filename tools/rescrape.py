import os
import sys
import time
import io
import argparse
import subprocess
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


def get_missing_researchers():
    conn = db()
    cur = conn.cursor()
    cur.execute('''
        SELECT u."UserID", u."FullName_Ar", u."Scholar_ID"
        FROM "Users" u
        LEFT JOIN "Authors" a ON a."UserID" = u."UserID"
        WHERE u."Scholar_ID" IS NOT NULL AND u."Scholar_ID" <> ''
          AND u."UserType" = 'Researcher'
        GROUP BY u."UserID", u."FullName_Ar", u."Scholar_ID"
        HAVING COUNT(a."PaperID") = 0
        ORDER BY u."UserID"
    ''')
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def get_paper_count(uid):
    conn = db()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM "Authors" WHERE "UserID" = %s', (uid,))
    n = cur.fetchone()[0]
    cur.close()
    conn.close()
    return n


def run_sync(scholar_id, uid):
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        ["python", "scrapers/scholar.py", scholar_id, str(uid)],
        capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env=env, timeout=600,
    )
    last_lines = (result.stdout or "").splitlines()[-8:]
    return result.returncode, last_lines, (result.stderr or "")[-300:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--start-from", type=int, default=None)
    ap.add_argument("--only", type=int, nargs="+", default=None)
    ap.add_argument("--exclude", type=int, nargs="+", default=None)
    ap.add_argument("--sleep", type=int, default=3)
    args = ap.parse_args()

    missing = get_missing_researchers()
    if args.start_from:
        missing = [m for m in missing if m[0] >= args.start_from]
    if args.only:
        only_set = set(args.only)
        missing = [m for m in missing if m[0] in only_set]
    if args.exclude:
        excl = set(args.exclude)
        missing = [m for m in missing if m[0] not in excl]
    if args.limit:
        missing = missing[:args.limit]

    print(f"Found {len(missing)} researchers with Scholar_ID but 0 papers\n")

    if args.dry_run:
        for uid, name, sid in missing:
            print(f"  UID={uid:3d}  Scholar={sid}  {name}")
        return

    n_success = n_zero = n_fail = 0
    for idx, (uid, name, scholar_id) in enumerate(missing, 1):
        print(f"\n[{idx}/{len(missing)}] UID={uid}  {name}")
        print(f"   Scholar={scholar_id}")
        try:
            rc, last_lines, stderr_tail = run_sync(scholar_id, uid)
            for line in last_lines:
                print(f"   {line}")
            if rc != 0:
                print(f"   exit code {rc}")
                if stderr_tail: print(f"   stderr: {stderr_tail}")
                n_fail += 1
            else:
                final_count = get_paper_count(uid)
                if final_count == 0:
                    print(f"   still 0 papers")
                    n_zero += 1
                else:
                    print(f"   {final_count} paper(s)")
                    n_success += 1
        except subprocess.TimeoutExpired:
            print(f"   timeout")
            n_fail += 1
        except Exception as e:
            print(f"   error: {e}")
            n_fail += 1

        if idx < len(missing):
            time.sleep(args.sleep)

    print(f"\nSummary: {n_success} succeeded, {n_zero} still empty, {n_fail} failed")


if __name__ == "__main__":
    main()
