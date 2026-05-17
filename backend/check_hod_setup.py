"""Diagnose: is the current logged-in user wired up as HoD of a department?"""
import os, sys, django

if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "litrix_backend.settings"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()

from django.db import connection

# UserID of عبدالكريم الزهراني (the one logged in)
USER_ID = 1

with connection.cursor() as cur:
    # 1. Is he set as HeadID anywhere?
    cur.execute("""
        SELECT "DepartmentID", "DepartmentName", "HeadID"
        FROM "Department"
        WHERE "HeadID" = %s
    """, [USER_ID])
    rows = cur.fetchall()
    print(f"Departments where UserID={USER_ID} is HeadID:")
    if rows:
        for r in rows:
            print(f"  DepartmentID={r[0]}  {r[1]}  HeadID={r[2]}")
    else:
        print(f"  (none — this is why HoD scoping returns NULL)")

    # 2. What permissions does his role have?
    cur.execute("""
        SELECT u."UserType", r."Name" AS role_name, u."RoleID"
        FROM "Users" u
        LEFT JOIN "Role" r ON r."RoleID" = u."RoleID"
        WHERE u."UserID" = %s
    """, [USER_ID])
    r = cur.fetchone()
    print(f"\nUser type: {r[0]}  role: {r[1]}  role_id: {r[2]}")

    # 3. What permissions does this role have?
    cur.execute("""
        SELECT p."Code"
        FROM "RolePermission" rp
        JOIN "Permission" p ON p."PermissionID" = rp."PermissionID"
        WHERE rp."RoleID" = %s
        ORDER BY p."Code"
    """, [r[2]])
    perms = [x[0] for x in cur.fetchall()]
    print(f"\nPermissions for this role:")
    for p in perms:
        print(f"  {p}")
    has_all = "view_all_researchers" in perms
    print(f"\nHas 'view_all_researchers'? {has_all}")
    print("(If True, HoD scoping is BYPASSED - role has admin-level perm)")

    # 4. All departments + their HeadIDs
    print("\n--- All departments ---")
    cur.execute('SELECT "DepartmentID", "DepartmentName", "HeadID" FROM "Department" ORDER BY 1')
    for r in cur.fetchall():
        print(f"  {r[0]:<4} {r[1]:<30} HeadID={r[2]}")
