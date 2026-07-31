"""Backfills Users.ScholarDisplayName - the exact name Google Scholar shows
on a researcher's profile (SerpAPI google_scholar_author -> author.name).
This is now the site's primary displayed name; FullName_Ar is never shown.

Usage (from backend/):
    python citations/backfill_scholar_names.py --dry-run
    python citations/backfill_scholar_names.py --yes
    python citations/backfill_scholar_names.py --force --yes   # re-fetch everyone
"""
import os
import sys
import time
import io
import argparse
from dotenv import load_dotenv
from serpapi import GoogleSearch

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
except Exception:
    pass

load_dotenv()
SERP_KEY = os.getenv("SERP_API_KEY")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from litrix_db import db


def fetch_scholar_name(scholar_id):
    """Returns the exact profile name Google Scholar displays, or None on
    any error / missing field."""
    try:
        params = {
            "engine": "google_scholar_author",
            "author_id": scholar_id,
            "api_key": SERP_KEY,
            "num": 1,
        }
        result = GoogleSearch(params).get_dict()
        name = ((result.get("author") or {}).get("name") or "").strip()
        return name or None
    except Exception:
        return None


def main():
    if not SERP_KEY:
        print("Missing SERP_API_KEY")
        sys.exit(1)

    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                     help="re-fetch everyone with a Scholar_ID, not just those missing a name")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true", help="skip the confirm prompt")
    args = ap.parse_args()

    conn = db()
    cur = conn.cursor()
    print(f"Connected to: {os.getenv('DATABASE_URL', 'LOCAL').split('@')[-1].split('/')[0]}\n")

    if args.force:
        cur.execute('''
            SELECT "UserID", "FullName_Ar", "Scholar_ID"
            FROM "Users"
            WHERE "Scholar_ID" IS NOT NULL AND "Scholar_ID" <> ''
            ORDER BY "UserID"
        ''')
    else:
        cur.execute('''
            SELECT "UserID", "FullName_Ar", "Scholar_ID"
            FROM "Users"
            WHERE "Scholar_ID" IS NOT NULL AND "Scholar_ID" <> ''
              AND ("ScholarDisplayName" IS NULL OR "ScholarDisplayName" = '')
            ORDER BY "UserID"
        ''')

    researchers = cur.fetchall()
    print(f"Researchers needing backfill: {len(researchers)}")
    print(f"SerpAPI credits to spend: {len(researchers)}\n")

    if args.dry_run:
        for uid, name, sid in researchers:
            print(f"  UID={uid:3d}  {sid}  {name}")
        return

    if not researchers:
        return

    if not args.yes:
        confirm = input("Proceed? [y/N]: ").strip().lower()
        if confirm != 'y':
            return

    n_filled = n_empty = n_failed = 0
    t_start = time.time()

    for i, (uid, name_ar, scholar_id) in enumerate(researchers, 1):
        time.sleep(0.4)
        try:
            scholar_name = fetch_scholar_name(scholar_id)
            if scholar_name:
                cur.execute(
                    'UPDATE "Users" SET "ScholarDisplayName" = %s WHERE "UserID" = %s',
                    (scholar_name, uid),
                )
                n_filled += 1
            else:
                n_empty += 1
        except Exception:
            n_failed += 1

        if i % 5 == 0:
            conn.commit()

        elapsed = time.time() - t_start
        rate = i / elapsed if elapsed > 0 else 0
        eta = (len(researchers) - i) / rate if rate > 0 else 0
        pct = int(100 * i / len(researchers))
        bar = '█' * (pct // 5) + '░' * (20 - pct // 5)
        print(f"  [{bar}] {pct:3d}%  {i}/{len(researchers)}  filled={n_filled} empty={n_empty} failed={n_failed}  ETA: {int(eta/60)}m{int(eta%60)}s",
              flush=True)

    conn.commit()
    print(f"\nDone — filled={n_filled} empty={n_empty} failed={n_failed}")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
