"""Quick check: does the Invitation table exist? What columns does it have?"""
import os, sys, django

if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "litrix_backend.settings"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()

from django.db import connection

with connection.cursor() as cur:
    cur.execute("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = 'Invitation'
        ORDER BY ordinal_position
    """)
    cols = cur.fetchall()

    if not cols:
        print("Invitation table does NOT exist.")
        print("Run: psql ... -f analytics/migrations/sprint6_invitations.sql")
        sys.exit(0)

    print("Invitation columns:")
    for name, dtype in cols:
        print(f"  {name:<25} {dtype}")

    # Check what roles exist for tenant 1
    cur.execute('SELECT "Name" FROM "Role" WHERE "TenantID" = 1 ORDER BY "Name"')
    roles = [r[0] for r in cur.fetchall()]
    print(f"\nRoles configured for tenant 1: {roles}")
