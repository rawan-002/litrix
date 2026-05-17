"""
Restore Arabic names for Ahlam + Abdulkareem.

Sets FullName_Ar, FirstName, MiddleName, LastName back to their
original Arabic values. Does NOT touch Email, UserType, AccountStatus,
or Department.HeadID — they stay "unregistered" stubs.

USAGE
  python restore_arabic_names.py        # dry-run
  python restore_arabic_names.py --apply
"""
import os, sys, argparse
import django

if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "litrix_backend.settings"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()

from django.db import connection

# (UserID, FullName_Ar, FirstName, MiddleName, LastName)
TARGETS = [
    (12, 'احلام التهامي محمد النابلي',
         'احلام', 'التهامي محمد', 'النابلي'),
    (1,  'عبدالكريم عوضه سعدي الحريري الزهراني',
         'عبدالكريم', 'عوضه سعدي الحريري', 'الزهراني'),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()

    print("=" * 70)
    print(f"Restore Arabic names  ({'APPLY' if args.apply else 'DRY-RUN'})")
    print("=" * 70)

    with connection.cursor() as c:
        # Show current state
        c.execute('''
            SELECT "UserID", "Litrix_ID", "FullName_Ar",
                   "FirstName", "MiddleName", "LastName",
                   "Email", "UserType", "AccountStatus"
              FROM "Users" WHERE "UserID" IN (1, 12)
             ORDER BY "UserID"
        ''')
        print("\nBefore:")
        for row in c.fetchall():
            print(" ", row)

        if not args.apply:
            print("\nWould set:")
            for uid, full_ar, fn, mn, ln in TARGETS:
                print(f"  UserID={uid}:")
                print(f"    FullName_Ar = {full_ar!r}")
                print(f"    FirstName   = {fn!r}")
                print(f"    MiddleName  = {mn!r}")
                print(f"    LastName    = {ln!r}")
            print("\nDRY RUN — nothing written. Re-run with --apply.")
            return

        # Apply
        for uid, full_ar, fn, mn, ln in TARGETS:
            c.execute('''
                UPDATE "Users"
                   SET "FullName_Ar" = %s,
                       "FirstName"   = %s,
                       "MiddleName"  = %s,
                       "LastName"    = %s
                 WHERE "UserID" = %s
            ''', [full_ar, fn, mn, ln, uid])
            print(f"  [ok] UserID={uid}: {full_ar}")

        # Show after
        c.execute('''
            SELECT "UserID", "Litrix_ID", "FullName_Ar",
                   "FirstName", "MiddleName", "LastName",
                   "Email", "UserType", "AccountStatus"
              FROM "Users" WHERE "UserID" IN (1, 12)
             ORDER BY "UserID"
        ''')
        print("\nAfter:")
        for row in c.fetchall():
            print(" ", row)


if __name__ == "__main__":
    main()
