"""
Apply a SQL migration file via Django's DB connection.

Why not psql? Some Windows dev setups don't have psql in PATH, and our
connection string already lives in Django settings - reusing it
guarantees we hit the same database the app uses.

USAGE:
    python apply_migration.py analytics/migrations/sprint6_invitations.sql
    python apply_migration.py analytics/migrations/sprint6_invitations.sql --dry-run
"""
import os, sys, argparse
import django

if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "litrix_backend.settings"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()

from django.db import connection, transaction


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sql_file", help="Path to .sql file to execute")
    ap.add_argument("--dry-run", action="store_true",
                    help="Run inside transaction then ROLLBACK")
    args = ap.parse_args()

    with open(args.sql_file, "r", encoding="utf-8") as f:
        sql = f.read()

    print(f"File   : {args.sql_file}")
    print(f"Size   : {len(sql)} bytes")
    print(f"Action : {'DRY RUN' if args.dry_run else 'EXECUTE'}")
    print("=" * 70)

    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(sql)
        if args.dry_run:
            transaction.set_rollback(True)
            print("\n[dry-run] rolled back. Nothing persisted.")
        else:
            print("\n[ok] migration applied.")


if __name__ == "__main__":
    main()
