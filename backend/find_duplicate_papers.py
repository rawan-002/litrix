"""
Detect duplicate papers in ResearchPaper - papers that should have been
deduplicated but weren't.

Strategies:
  1. Same DOI (must be unique by definition)
  2. Same NormalizedTitle
  3. Same lowercase Title (after trim)
  4. Authors rows pointing to multiple UserIDs for the same PaperID
     (indicates claim-merge issues)

USAGE:
    python find_duplicate_papers.py
    python find_duplicate_papers.py --user 1   # focus on one researcher
"""
import os, sys, argparse
import django
if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "litrix_backend.settings"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()
from django.db import connection


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", type=int, help="Focus on one UserID")
    args = ap.parse_args()

    user_filter = ""
    user_params = []
    if args.user:
        user_filter = (
            'WHERE rp."PaperID" IN ('
            '  SELECT "PaperID" FROM "Authors" WHERE "UserID" = %s'
            ')'
        )
        user_params = [args.user]

    with connection.cursor() as cur:

        # 1. Duplicates by DOI (shouldn't happen due to UNIQUE constraint, but check)
        cur.execute(f"""
            SELECT "DOI", COUNT(*) AS n, ARRAY_AGG("PaperID" ORDER BY "PaperID")
            FROM "ResearchPaper" rp
            {user_filter}
            {'AND' if user_filter else 'WHERE'} "DOI" IS NOT NULL AND "DOI" <> ''
            GROUP BY "DOI"
            HAVING COUNT(*) > 1
            ORDER BY n DESC
            LIMIT 20
        """, user_params)
        print("=== Duplicates by DOI ===")
        rows = cur.fetchall()
        if not rows:
            print("  None")
        for doi, n, ids in rows:
            print(f"  {n}x  DOI={doi}  PaperIDs={ids}")

        # 2. Duplicates by NormalizedTitle
        cur.execute(f"""
            SELECT "NormalizedTitle", COUNT(*) AS n,
                   ARRAY_AGG("PaperID" ORDER BY "PaperID"),
                   ARRAY_AGG(LEFT("Title", 60) ORDER BY "PaperID")
            FROM "ResearchPaper" rp
            {user_filter}
            {'AND' if user_filter else 'WHERE'} "NormalizedTitle" IS NOT NULL
              AND "NormalizedTitle" <> ''
            GROUP BY "NormalizedTitle"
            HAVING COUNT(*) > 1
            ORDER BY n DESC
            LIMIT 20
        """, user_params)
        print("\n=== Duplicates by NormalizedTitle ===")
        rows = cur.fetchall()
        if not rows:
            print("  None")
        for nt, n, ids, titles in rows:
            print(f"  {n}x  PaperIDs={ids}")
            for t in titles:
                print(f"      title: {t}")

        # 3. NormalizedTitle is NULL on old papers - count + sample
        cur.execute(f"""
            SELECT COUNT(*)
            FROM "ResearchPaper" rp
            {user_filter}
            {'AND' if user_filter else 'WHERE'} ("NormalizedTitle" IS NULL OR "NormalizedTitle" = '')
        """, user_params)
        null_norm = cur.fetchone()[0]
        print(f"\n=== Papers with NULL/empty NormalizedTitle: {null_norm} ===")
        print("  (These bypass the scraper's dedup check.)")
        if null_norm:
            cur.execute(f"""
                SELECT "PaperID", LEFT("Title", 60), "DOI", "Source"
                FROM "ResearchPaper" rp
                {user_filter}
                {'AND' if user_filter else 'WHERE'} ("NormalizedTitle" IS NULL OR "NormalizedTitle" = '')
                LIMIT 5
            """, user_params)
            print("  Sample:")
            for pid, t, doi, src in cur.fetchall():
                print(f"    PaperID={pid}  src={src}  doi={doi}")
                print(f"      title: {t}")

        # 4. Authors entries with multiple UserIDs per PaperID
        cur.execute("""
            SELECT a."PaperID", COUNT(DISTINCT a."UserID") AS n_users,
                   ARRAY_AGG(DISTINCT a."UserID" ORDER BY a."UserID")
            FROM "Authors" a
            GROUP BY a."PaperID"
            HAVING COUNT(DISTINCT a."UserID") > 1
            ORDER BY n_users DESC
            LIMIT 10
        """)
        print("\n=== PaperIDs with multiple linked UserIDs ===")
        print("  (Normal: co-authored papers. Suspicious: duplicate links.)")
        rows = cur.fetchall()
        if not rows:
            print("  None")
        for pid, n, uids in rows[:5]:
            print(f"  PaperID={pid}  {n} UserIDs: {uids}")


if __name__ == "__main__":
    main()
