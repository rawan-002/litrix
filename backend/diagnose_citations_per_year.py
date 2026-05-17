"""
Diagnose citation-per-year coverage across the DB.

Checks:
  1. % of papers with non-null CitationsByYear
  2. % of researchers with non-null Researcher.CitationsByYear
  3. Per-source breakdown (Scholar vs OpenAlex vs Manual)
  4. Sample 5 researchers and show their per-paper coverage
"""
import os, sys
import django

if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "litrix_backend.settings"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()

from django.db import connection


def pct(part, whole):
    return f"{(100.0 * part / whole):.1f}%" if whole else "—"


with connection.cursor() as cur:
    # 1. Paper-level coverage
    cur.execute('SELECT COUNT(*) FROM "ResearchPaper"')
    total_papers = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(*) FROM "ResearchPaper"
        WHERE "CitationsByYear" IS NOT NULL
          AND "CitationsByYear" <> '{}'::jsonb
    """)
    with_cby = cur.fetchone()[0]

    print(f"Total papers           : {total_papers}")
    print(f"With CitationsByYear   : {with_cby}  ({pct(with_cby, total_papers)})")
    print()

    # 2. Per-source breakdown
    print("Per-source coverage:")
    cur.execute("""
        SELECT
            COALESCE("Source", '(null)') AS src,
            COUNT(*)                                                AS total,
            COUNT(*) FILTER (
                WHERE "CitationsByYear" IS NOT NULL
                  AND "CitationsByYear" <> '{}'::jsonb
            ) AS with_cby
        FROM "ResearchPaper"
        GROUP BY "Source"
        ORDER BY 2 DESC
    """)
    for src, total, w in cur.fetchall():
        print(f"  {src:<12} {w:>5}/{total:<5}  ({pct(w, total)})")
    print()

    # 3. Researcher-level coverage
    cur.execute('SELECT COUNT(*) FROM "Researcher"')
    total_researchers = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(*) FROM "Researcher"
        WHERE "CitationsByYear" IS NOT NULL
          AND "CitationsByYear" <> '{}'::jsonb
    """)
    with_rcby = cur.fetchone()[0]

    print(f"Total researchers          : {total_researchers}")
    print(f"With Researcher.CitationsByYear : "
          f"{with_rcby}  ({pct(with_rcby, total_researchers)})")
    print()

    # 4. Sample of researchers and their paper coverage
    cur.execute("""
        SELECT
            u."UserID",
            u."FullName_Ar",
            (SELECT COUNT(*) FROM "Authors" a
             WHERE a."UserID" = u."UserID")           AS total,
            (SELECT COUNT(*) FROM "Authors" a
             JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
             WHERE a."UserID" = u."UserID"
               AND rp."CitationsByYear" IS NOT NULL
               AND rp."CitationsByYear" <> '{}'::jsonb) AS with_cby,
            r."CitationsByYear" IS NOT NULL           AS has_researcher_cby
        FROM "Users" u
        JOIN "Researcher" r ON r."UserID" = u."UserID"
        WHERE u."UserType" = 'Researcher'
        ORDER BY total DESC
        LIMIT 10
    """)
    print("Top 10 researchers by paper count:")
    print(f"  {'UserID':<7} {'name':<35} {'papers':>7} {'with cby':>9}  rsrcher_cby")
    for r in cur.fetchall():
        name = (r[1] or '')[:33]
        print(f"  {r[0]:<7} {name:<35} {r[2]:>7} {r[3]:>9}  {r[4]}")
