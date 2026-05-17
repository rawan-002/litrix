"""Diagnostic: which columns hold ORCID across Users and Researcher tables?"""
import os, sys
import django
if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "litrix_backend.settings"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()
from django.db import connection

with connection.cursor() as cur:
    print("=== ORCID-like columns ===")
    cur.execute("""
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND LOWER(column_name) LIKE '%orcid%'
        ORDER BY table_name, column_name
    """)
    cols = cur.fetchall()
    for t, c in cols:
        print(f"  {t:20s}.{c}")

    print("\n=== Non-null counts per column ===")
    for t, c in cols:
        cur.execute(f'SELECT COUNT(*) FROM "{t}" WHERE "{c}" IS NOT NULL AND "{c}" <> \'\'')
        n = cur.fetchone()[0]
        print(f'  {t:20s}.{c}  ->  {n}')

    print("\n=== Sample of Users with ORCID ===")
    # Try both column names
    for col in ("Orcid_ID", "ORCID"):
        try:
            cur.execute(f"""
                SELECT "UserID", "FullName_Ar", "{col}"
                FROM "Users"
                WHERE "{col}" IS NOT NULL AND "{col}" <> ''
                LIMIT 5
            """)
            rows = cur.fetchall()
            print(f"\nUsers.{col}: {len(rows)} samples")
            for r in rows:
                print(f"  UserID={r[0]} name={r[1]} orcid={r[2]}")
        except Exception as e:
            print(f"\nUsers.{col}: ERROR - {e}")
