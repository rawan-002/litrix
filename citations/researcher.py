import os
import sys
import time
import io
import argparse
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import Json
from serpapi import GoogleSearch

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
except Exception:
    pass

load_dotenv()
SERP_KEY = os.getenv("SERP_API_KEY")


# Shared DB helper (single source — see litrix_db.py at repo root).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from litrix_db import db


def fetch_cited_by_graph(scholar_id):
    try:
        params = {
            "engine": "google_scholar_author",
            "author_id": scholar_id,
            "api_key": SERP_KEY,
            "num": 1,
        }
        result = GoogleSearch(params).get_dict()
        cb = result.get("cited_by") or {}
        graph = cb.get("graph") or []
        return {
            str(g.get("year")): int(g.get("citations") or 0)
            for g in graph if g.get("year") is not None
        }
    except Exception:
        return None


def main():
    if not SERP_KEY:
        print("Missing SERP_API_KEY")
        sys.exit(1)

    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    conn = db()
    cur = conn.cursor()
    print(f"Connected to: {os.getenv('DATABASE_URL', 'LOCAL').split('@')[-1].split('/')[0]}\n")

    if args.force:
        cur.execute('''
            SELECT u."UserID", u."FullName_Ar", u."Scholar_ID"
            FROM "Users" u
            WHERE u."Scholar_ID" IS NOT NULL AND u."Scholar_ID" <> ''
              AND u."UserType" = 'Researcher'
            ORDER BY u."UserID"
        ''')
    else:
        cur.execute('''
            SELECT u."UserID", u."FullName_Ar", u."Scholar_ID"
            FROM "Users" u
            LEFT JOIN "Researcher" r ON r."UserID" = u."UserID"
            WHERE u."Scholar_ID" IS NOT NULL AND u."Scholar_ID" <> ''
              AND u."UserType" = 'Researcher'
              AND (r."CitationsByYear" IS NULL
                   OR r."CitationsByYear"::text = '{}'
                   OR r."UserID" IS NULL)
            ORDER BY u."UserID"
        ''')

    researchers = cur.fetchall()
    if args.limit:
        researchers = researchers[:args.limit]

    print(f"Researchers needing backfill: {len(researchers)}")
    print(f"SerpAPI credits to spend: {len(researchers)}\n")

    if args.dry_run:
        for uid, name, sid in researchers:
            print(f"  UID={uid:3d}  {sid}  {name}")
        return

    if not researchers:
        return

    confirm = input("Proceed? [y/N]: ").strip().lower()
    if confirm != 'y':
        return

    n_filled = n_empty = n_failed = 0
    t_start = time.time()

    for i, (uid, name, scholar_id) in enumerate(researchers, 1):
        time.sleep(0.4)
        try:
            graph = fetch_cited_by_graph(scholar_id)
            if graph is None:
                n_failed += 1
            elif not graph:
                n_empty += 1
            else:
                cur.execute('''
                    INSERT INTO "Researcher" ("UserID", "CitationsByYear", "LastSyncedAt")
                    VALUES (%s, %s, NOW())
                    ON CONFLICT ("UserID")
                    DO UPDATE SET "CitationsByYear" = EXCLUDED."CitationsByYear",
                                  "LastSyncedAt"   = NOW()
                ''', (uid, Json(graph)))
                n_filled += 1
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
