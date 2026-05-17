"""Quick verify: which emails are currently in Users table for our test accounts."""
import os, sys, django
if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "litrix_backend.settings"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()
from django.db import connection

TARGETS = ["ra20awn@gmail.com", "rahmat@bu.edu.sa", "moha@gmail.com"]

with connection.cursor() as cur:
    print("State of the 3 test emails:")
    print("-" * 70)
    for email in TARGETS:
        cur.execute("""
            SELECT "UserID", "Email", "FullName_Ar", "AccountStatus"
            FROM "Users"
            WHERE LOWER("Email") = LOWER(%s)
        """, [email])
        rows = cur.fetchall()
        if rows:
            for r in rows:
                print(f"  STILL THERE: UserID={r[0]}  {r[1]}  ({r[2]})  status={r[3]}")
        else:
            print(f"  cleaned       : {email}  -> no longer in DB")

    # Also check what user UserID=1 looks like now
    cur.execute('''
        SELECT "UserID", "Email", "FullName_Ar", "AccountStatus"
        FROM "Users" WHERE "UserID" = 1
    ''')
    r = cur.fetchone()
    print("")
    print(f"  UserID=1 current state:")
    print(f"    Email   : {r[1]}")
    print(f"    Name    : {r[2]}")
    print(f"    Status  : {r[3]}")
