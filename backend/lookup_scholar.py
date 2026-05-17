"""Lookup who owns a given Scholar_ID in the Users table."""
import os, sys, django
if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "litrix_backend.settings"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()
from django.db import connection

SCHOLAR_ID = sys.argv[1] if len(sys.argv) > 1 else "JSQbyBgAAAAJ"

with connection.cursor() as cur:
    cur.execute("""
        SELECT u."UserID", u."Email", u."FullName_Ar", u."FirstName",
               u."LastName", u."AccountStatus", u."Scholar_ID",
               (SELECT COUNT(*) FROM "Authors" a
                WHERE a."UserID" = u."UserID")  AS papers_count
        FROM "Users" u
        WHERE u."Scholar_ID" = %s
    """, [SCHOLAR_ID])
    rows = cur.fetchall()
    print(f"Searching Scholar_ID = {SCHOLAR_ID}")
    print("=" * 70)
    if not rows:
        print("  No match.")
    for r in rows:
        print(f"  UserID={r[0]}")
        print(f"    Email     : {r[1]}")
        print(f"    Name (ar) : {r[2]}")
        print(f"    Name (en) : {r[3] or ''} {r[4] or ''}")
        print(f"    Status    : {r[5]}")
        print(f"    Papers    : {r[7]}")
