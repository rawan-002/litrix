import os
import sys
import io
import argparse
from dotenv import load_dotenv
import psycopg2

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
except Exception:
    pass

load_dotenv()


# Shared DB helper (single source — see litrix_db.py at repo root).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from litrix_db import db


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sql_file")
    args = ap.parse_args()

    if not os.path.exists(args.sql_file):
        print(f"File not found: {args.sql_file}")
        sys.exit(1)

    with open(args.sql_file, encoding='utf-8') as f:
        sql = f.read()

    print(f"Connected to: {os.getenv('DATABASE_URL', 'LOCAL').split('@')[-1].split('/')[0]}")
    print(f"Running: {args.sql_file}\n")

    conn = db()
    cur = conn.cursor()
    try:
        cur.execute(sql)
        conn.commit()
        print("Migration applied successfully")
    except Exception as e:
        conn.rollback()
        print(f"Migration failed: {e}")
        sys.exit(1)
    finally:
        cur.close()
        conn.close()

    cur = db().cursor()
    print("\n=== Verification ===")
    for table in ['Tenant', 'Role', 'Permission', 'RolePermission',
                  'EmailVerification', 'RegistrationRequest', 'Notification',
                  'AuditLog', 'RefreshToken']:
        cur.execute(f'SELECT COUNT(*) FROM "{table}"')
        n = cur.fetchone()[0]
        print(f"  {table}: {n} rows")

    cur.execute('SELECT "Name", "TenantID" FROM "Tenant"')
    for r in cur.fetchall():
        print(f"\n  Tenant #{r[1]}: {r[0]}")

    cur.execute('''
        SELECT r."Name", COUNT(rp."PermissionID") AS perms
        FROM "Role" r
        LEFT JOIN "RolePermission" rp ON rp."RoleID" = r."RoleID"
        GROUP BY r."RoleID", r."Name"
        ORDER BY r."RoleID"
    ''')
    print("\n  Roles:")
    for r in cur.fetchall():
        print(f"    {r[0]:12s} {r[1]} permissions")


if __name__ == "__main__":
    main()
