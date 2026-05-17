"""
Assign a user as Head of Department.

USAGE:
    python set_hod.py <user_id> <department_id>
    python set_hod.py 1 3        # عبدالكريم → علوم الحاسبات
"""
import os, sys, django
if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "litrix_backend.settings"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()

from django.db import connection, transaction

if len(sys.argv) < 3:
    print("Usage: python set_hod.py <user_id> <department_id>")
    sys.exit(1)

user_id   = int(sys.argv[1])
dept_id   = int(sys.argv[2])

with transaction.atomic():
    with connection.cursor() as cur:
        # 1. Verify user exists + is HoD type
        cur.execute("""
            SELECT "FullName_Ar", "UserType" FROM "Users" WHERE "UserID" = %s
        """, [user_id])
        u = cur.fetchone()
        if not u:
            print(f"User {user_id} not found.")
            sys.exit(1)
        print(f"User : {u[0]}  (type={u[1]})")

        # 2. Verify department exists
        cur.execute("""
            SELECT "DepartmentName", "HeadID" FROM "Department" WHERE "DepartmentID" = %s
        """, [dept_id])
        d = cur.fetchone()
        if not d:
            print(f"Department {dept_id} not found.")
            sys.exit(1)
        print(f"Dept : {d[0]}  (current HeadID={d[1]})")

        # 3. Set HeadID
        cur.execute("""
            UPDATE "Department" SET "HeadID" = %s WHERE "DepartmentID" = %s
        """, [user_id, dept_id])
        print(f"\nSet HeadID={user_id} on DepartmentID={dept_id}.")
        print("\nAll departments after update:")
        cur.execute('SELECT "DepartmentID", "DepartmentName", "HeadID" FROM "Department" ORDER BY 1')
        for r in cur.fetchall():
            mark = "  <-- now HoD" if r[2] == user_id else ""
            print(f"  {r[0]} {r[1]:<25} HeadID={r[2]}{mark}")
