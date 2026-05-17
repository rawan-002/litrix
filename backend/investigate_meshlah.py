"""
Investigate why محمد ابراهيم يعن الله آل مشلح jumped from 708 to 811.

Shows:
  - Total papers linked by MappingCriteria (how were they discovered?)
  - Sample of the 'orcid_backfill' papers (these are the new ones)
  - Whether his ORCID/Scholar_ID matches the papers' metadata
"""
import os, sys
import django

if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "litrix_backend.settings"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()

from django.db import connection

with connection.cursor() as cur:
    # 1. Find his UserID
    cur.execute("""
        SELECT "UserID", "FullName_Ar", "Scholar_ID", "Orcid_ID"
        FROM "Users"
        WHERE "FullName_Ar" LIKE '%مشلح%'
           OR "FullName_Ar" LIKE '%يعن الله%'
        LIMIT 5
    """)
    rows = cur.fetchall()
    if not rows:
        print("User not found.")
        sys.exit(0)

    print("Candidate users:")
    for r in rows:
        print(f"  UserID={r[0]}  Name={r[1]}  Scholar={r[2]}  ORCID={r[3]}")

    user_id = rows[0][0]
    print(f"\nUsing UserID={user_id}\n")

    # 2. Total papers + breakdown by MappingCriteria
    cur.execute("""
        SELECT
            "MappingCriteria",
            COUNT(*),
            AVG("MappingConfidence")::numeric(4,3)
        FROM "Authors"
        WHERE "UserID" = %s
        GROUP BY "MappingCriteria"
        ORDER BY 2 DESC
    """, [user_id])
    print("Papers by MappingCriteria:")
    total = 0
    for crit, n, conf in cur.fetchall():
        print(f"  {crit:30s} -> {n:>5}  (avg confidence {conf})")
        total += n
    print(f"  {'TOTAL':30s} -> {total}")
    print()

    # 3. Sample 10 papers linked via orcid_backfill (the suspected ones)
    cur.execute("""
        SELECT
            rp."PaperID",
            rp."Title",
            rp."PubYear",
            rp."Source",
            a."AuthorNameRaw",
            a."MappingConfidence",
            jsonb_path_query_array(
                rp."RawData_Log"->'authorships',
                '$[*].author.orcid'
            ) AS authorship_orcids
        FROM "Authors" a
        JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
        WHERE a."UserID" = %s
          AND a."MappingCriteria" = 'orcid_backfill'
        ORDER BY rp."PubYear" DESC NULLS LAST
        LIMIT 10
    """, [user_id])

    print("Sample of papers linked via orcid_backfill:")
    for r in cur.fetchall():
        print(f"\n  PaperID={r[0]}  Year={r[2]}  Source={r[3]}")
        print(f"    Title       : {(r[1] or '')[:90]}")
        print(f"    Scraped name: {r[4]}")
        print(f"    ORCIDs in paper: {r[6]}")

    # 4. Does his ORCID actually appear in those papers' authorships?
    print("\n\nSanity check: how many orcid_backfill papers actually contain his ORCID?")
    cur.execute("""
        SELECT u."Orcid_ID"
        FROM "Users" u
        WHERE u."UserID" = %s
    """, [user_id])
    his_orcid = cur.fetchone()[0]
    print(f"  His ORCID: {his_orcid}")

    if his_orcid:
        cur.execute("""
            SELECT
                COUNT(*) FILTER (
                    WHERE EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements(rp."RawData_Log"->'authorships') ship
                        WHERE REPLACE(
                                REPLACE(ship->'author'->>'orcid', 'https://orcid.org/', ''),
                                'http://orcid.org/', ''
                              ) = %s
                    )
                ) AS contains_his_orcid,
                COUNT(*) AS total_backfilled
            FROM "Authors" a
            JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
            WHERE a."UserID" = %s
              AND a."MappingCriteria" = 'orcid_backfill'
        """, [his_orcid, user_id])
        c, t = cur.fetchone()
        print(f"  Backfilled papers containing his ORCID: {c}/{t}")
        if c == t:
            print("  -> ALL of them have his ORCID. Links are correct.")
        else:
            print(f"  -> {t - c} papers do NOT contain his ORCID. Possible mis-link!")
