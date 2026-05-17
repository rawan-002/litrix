"""
Show ALL Users and ALL RegistrationRequest rows so you can spot the
test accounts you made (by date, name, status, etc.).

Read-only - doesn't touch the DB.

USAGE:
    python list_test_accounts.py
    python list_test_accounts.py --limit 30
    python list_test_accounts.py --recent-only
"""
import os, sys, argparse
import django

if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "litrix_backend.settings"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()

from django.db import connection


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50,
                    help="Show only the N most recent rows of each table")
    ap.add_argument("--recent-only", action="store_true",
                    help="Show only rows created in the last 30 days")
    args = ap.parse_args()

    where_recent = (
        'WHERE "CreatedAt" >= NOW() - INTERVAL \'30 days\''
        if args.recent_only else ""
    )
    where_recent_reg = (
        'WHERE "SubmittedAt" >= NOW() - INTERVAL \'30 days\''
        if args.recent_only else ""
    )

    with connection.cursor() as cur:

        # ---- All Users, newest first ----
        print("=" * 95)
        print(f"  USERS  (showing {args.limit} most recent)")
        print("=" * 95)
        cur.execute(f'''
            SELECT "UserID", "Email", "FullName_Ar", "UserType",
                   "AccountStatus", "CreatedAt"
            FROM "Users"
            {where_recent}
            ORDER BY "CreatedAt" DESC NULLS LAST
            LIMIT %s
        ''', [args.limit])
        users = cur.fetchall()
        print(f"\n{'UserID':<8}{'Type':<14}{'Status':<12}{'Created':<22}"
              f"{'Email':<35}Name")
        print("-" * 95)
        for r in users:
            uid, email, name, utype, st, created = r
            email = (email or '')[:33]
            name  = (name or '')[:30]
            utype = (utype or '')[:12]
            st    = (st or '')[:10]
            created = str(created)[:19] if created else ''
            print(f"{uid:<8}{utype:<14}{st:<12}{created:<22}{email:<35}{name}")

        # ---- All RegistrationRequests, newest first ----
        print("\n" + "=" * 95)
        print(f"  REGISTRATION REQUESTS  (showing {args.limit} most recent)")
        print("=" * 95)
        cur.execute(f'''
            SELECT "RequestID", "Email", "FullName_Ar", "Status",
                   "SubmittedAt"
            FROM "RegistrationRequest"
            {where_recent_reg}
            ORDER BY "SubmittedAt" DESC NULLS LAST
            LIMIT %s
        ''', [args.limit])
        regs = cur.fetchall()
        print(f"\n{'ReqID':<8}{'Status':<12}{'Submitted':<22}"
              f"{'Email':<35}Name")
        print("-" * 95)
        for r in regs:
            rid, email, name, st, sub = r
            email = (email or '')[:33]
            name  = (name or '')[:30]
            st    = (st or '')[:10]
            sub   = str(sub)[:19] if sub else ''
            print(f"{rid:<8}{st:<12}{sub:<22}{email:<35}{name}")

        # ---- Summary ----
        cur.execute('SELECT COUNT(*) FROM "Users"')
        total_users = cur.fetchone()[0]
        cur.execute('SELECT COUNT(*) FROM "RegistrationRequest"')
        total_regs = cur.fetchone()[0]
        print()
        print("=" * 95)
        print(f"  Total Users in DB              : {total_users}")
        print(f"  Total RegistrationRequest rows : {total_regs}")
        print("=" * 95)
        print()
        print("Pick out the test rows by UserID / RequestID. Send me the")
        print("list and I'll write a precise delete script.")


if __name__ == "__main__":
    main()
