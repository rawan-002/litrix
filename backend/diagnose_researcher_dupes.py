"""
Diagnose duplicates for a specific researcher (by Scholar_ID).
Shows their paper counts, suspected dupes, and source breakdown.
"""
import os, sys
import django
if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "litrix_backend.settings"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()
from django.db import connection

SCHOLAR_ID = sys.argv[1] if len(sys.argv) > 1 else "lURTCEIAAAAJ"

with connection.cursor() as cur:
    # 1. Find the user
    cur.execute("""
        SELECT "UserID", "FullName_Ar", "Scholar_ID", "AccountStatus"
        FROM "Users" WHERE "Scholar_ID" = %s
    """, [SCHOLAR_ID])
    rows = cur.fetchall()
    print(f"Looking up Scholar_ID = {SCHOLAR_ID}")
    print("=" * 70)
    if not rows:
        print("  No user with this Scholar_ID")
        sys.exit(0)
    for r in rows:
        print(f"  UserID={r[0]}  {r[1]}  status={r[3]}")

    user_id = rows[0][0]

    # 2. Count their papers + by source
    cur.execute("""
        SELECT rp."Source", COUNT(*)
        FROM "Authors" a
        JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
        WHERE a."UserID" = %s
        GROUP BY rp."Source"
        ORDER BY 2 DESC
    """, [user_id])
    print(f"\nPaper count for UserID={user_id} by Source:")
    total = 0
    for src, n in cur.fetchall():
        print(f"  {src or '(null)':<15} {n}")
        total += n
    print(f"  {'TOTAL':<15} {total}")

    # 3. Find dupes by NormalizedTitle WITHIN this user's papers
    cur.execute("""
        SELECT rp."NormalizedTitle", COUNT(*) AS n,
               ARRAY_AGG(rp."PaperID" ORDER BY rp."PaperID") AS pids,
               ARRAY_AGG(LEFT(rp."Title", 50) ORDER BY rp."PaperID") AS titles,
               ARRAY_AGG(rp."Source" ORDER BY rp."PaperID") AS sources,
               ARRAY_AGG(rp."DOI" ORDER BY rp."PaperID") AS dois
        FROM "Authors" a
        JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
        WHERE a."UserID" = %s
          AND rp."NormalizedTitle" IS NOT NULL
          AND rp."NormalizedTitle" <> ''
        GROUP BY rp."NormalizedTitle"
        HAVING COUNT(*) > 1
        ORDER BY n DESC
        LIMIT 10
    """, [user_id])
    print(f"\nDuplicates within this user's papers (same NormalizedTitle):")
    rows = cur.fetchall()
    if not rows:
        print("  None")
    for nt, n, pids, titles, sources, dois in rows:
        print(f"\n  {n}x  PaperIDs={list(pids)}")
        for t, s, d in zip(titles, sources, dois):
            print(f"      src={s:<10} doi={d or '-':<25} title: {t}")

    # 4. Find dupes by Title (lowercase) - catches NULL NormalizedTitle cases
    cur.execute("""
        SELECT LOWER(TRIM(rp."Title")) AS lt, COUNT(*) AS n,
               ARRAY_AGG(rp."PaperID" ORDER BY rp."PaperID") AS pids,
               ARRAY_AGG(rp."Source" ORDER BY rp."PaperID") AS sources
        FROM "Authors" a
        JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
        WHERE a."UserID" = %s
          AND rp."Title" IS NOT NULL
        GROUP BY LOWER(TRIM(rp."Title"))
        HAVING COUNT(*) > 1
        ORDER BY n DESC
        LIMIT 10
    """, [user_id])
    print(f"\nDuplicates by lowercase Title:")
    rows = cur.fetchall()
    if not rows:
        print("  None")
    for lt, n, pids, sources in rows:
        print(f"  {n}x  PaperIDs={list(pids)}  sources={list(sources)}")
        print(f"      title: {lt[:80]}")
