"""Print SUSPICIOUS ORCID rows in detail so we can spot real vs false positives."""
import os, sys
import django
from collections import Counter
import re

if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "litrix_backend.settings"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()
from django.db import connection


def norm(s):
    if not s: return ""
    s = s.lower()
    s = re.sub(r"[.,\-_']", " ", s)
    return re.sub(r"\s+", " ", s).strip()


with connection.cursor() as cur:
    cur.execute("""
        SELECT u."UserID", u."FullName_Ar", u."FirstName", u."LastName",
               r."ORCID_ID"
        FROM "Researcher" r
        JOIN "Users" u ON u."UserID" = r."UserID"
        WHERE r."ORCID_ID" IS NOT NULL AND r."ORCID_ID" <> ''
        ORDER BY u."UserID"
    """)
    researchers = cur.fetchall()

    print("=" * 90)
    for user_id, name_ar, fn, ln, orcid in researchers:
        cur.execute("""
            SELECT ship->'author'->>'display_name'
            FROM "ResearchPaper" rp
            CROSS JOIN LATERAL jsonb_array_elements(rp."RawData_Log"->'authorships') AS ship
            WHERE REPLACE(REPLACE(ship->'author'->>'orcid','https://orcid.org/',''),
                          'http://orcid.org/','') = %s
        """, [orcid])
        names = [r[0] for r in cur.fetchall() if r[0]]
        if not names:
            continue

        counter = Counter(names)
        top3 = counter.most_common(3)
        reg_en = norm(f"{fn or ''} {ln or ''}")
        # Use a more lenient check: any common token of length >= 4
        # between registered English name and most-common scraped name
        most_common = top3[0][0]
        scraped_norm = norm(most_common)
        common_tokens = set(reg_en.split()) & set(scraped_norm.split())
        long_common = [t for t in common_tokens if len(t) >= 4]
        verdict = "LIKELY_CORRECT" if long_common else "SUSPICIOUS"

        if verdict == "SUSPICIOUS":
            print(f"\nUserID={user_id}  Papers={len(names)}")
            print(f"  Litrix Ar : {name_ar}")
            print(f"  Litrix En : {fn} {ln}")
            print(f"  ORCID     : {orcid}")
            print(f"  Top scraped names:")
            for nm, cnt in top3:
                print(f"    [{cnt:>3}x]  {nm}")
