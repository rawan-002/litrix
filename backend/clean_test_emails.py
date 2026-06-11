"""
Clean test-only email + password from specific user accounts.

Uses one savepoint per user so a per-row failure doesn't poison the
entire transaction (which is what Postgres does on the first SQL error
inside an explicit transaction).
"""
import os, sys, json, argparse
import django

if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "litrix_backend.settings"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()

from django.db import connection, transaction


TARGET_EMAILS = [
    "ra20awn@gmail.com",
    "rahmat@bu.edu.sa",
    "moha@gmail.com",
]


def safe_delete(cur, sql, params):
    """Try a DELETE; rollback only that one if the table is missing
    or any other error occurs. Uses a savepoint so the parent tx
    stays alive."""
    cur.execute("SAVEPOINT sp_optional")
    try:
        cur.execute(sql, params)
        cur.execute("RELEASE SAVEPOINT sp_optional")
    except Exception:
        cur.execute("ROLLBACK TO SAVEPOINT sp_optional")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with transaction.atomic():
        with connection.cursor() as cur:

            n_cleaned   = 0
            n_not_found = 0

            for email in TARGET_EMAILS:
                # Per-user savepoint - if anything fails, only this
                # user's changes roll back. The rest still process.
                cur.execute("SAVEPOINT sp_user")
                try:
                    cur.execute("""
                        SELECT "UserID", "Email", "FullName_Ar",
                               "PasswordHash", "EmailVerified", "AccountStatus"
                        FROM "Users"
                        WHERE LOWER("Email") = LOWER(%s)
                        LIMIT 1
                    """, [email])
                    row = cur.fetchone()
                    if not row:
                        print(f"  [skip] no Users row with email {email}")
                        n_not_found += 1
                        cur.execute("RELEASE SAVEPOINT sp_user")
                        continue

                    user_id, old_email, name_ar, old_pwd, old_verified, old_status = row
                    placeholder = f"unverified_{user_id}@litrix.local"

                    print(f"\n  UserID={user_id}  ({name_ar})")
                    print(f"    old email   : {old_email}")
                    print(f"    new email   : {placeholder}")

                    # Snapshot to AuditLog
                    snapshot = {
                        "UserID":         user_id,
                        "FullName_Ar":    name_ar,
                        "old_email":      old_email,
                        "had_password":   bool(old_pwd),
                        "old_status":     old_status,
                        "old_verified":   old_verified,
                        "reason":         "test_account_cleanup",
                    }
                    cur.execute(
                        'INSERT INTO "AuditLog" '
                        '("TenantID","UserID","Action","TargetType","TargetID",'
                        ' "Metadata","IpAddress","UserAgent") '
                        'VALUES (1, NULL, %s, %s, %s, %s::jsonb, NULL, %s)',
                        ["user.clean.test_email", "Users", user_id,
                         json.dumps(snapshot),
                         "cleanup_script:clean_test_emails"]
                    )

                    # Reset auth state
                    cur.execute("""
                        UPDATE "Users"
                        SET "Email"          = %s,
                            "PasswordHash"   = NULL,
                            "EmailVerified"  = FALSE,
                            "AccountStatus"  = 'Pending'
                        WHERE "UserID" = %s
                    """, [placeholder, user_id])

                    # Optional cascade cleanups - each in its own savepoint
                    safe_delete(
                        cur,
                        'DELETE FROM "EmailVerification" WHERE "UserID" = %s',
                        [user_id],
                    )
                    safe_delete(
                        cur,
                        'DELETE FROM "PasswordResetToken" WHERE "UserID" = %s',
                        [user_id],
                    )

                    cur.execute("RELEASE SAVEPOINT sp_user")
                    print(f"    cleaned OK")
                    n_cleaned += 1

                except Exception as exc:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_user")
                    print(f"  [error] {email}: {exc}")

            print("")
            print(f"  Cleaned   : {n_cleaned}")
            print(f"  Not found : {n_not_found}")

            if args.dry_run:
                transaction.set_rollback(True)
                print("\n  --dry-run: rolled back. Nothing written.")
            else:
                print("\n  Done. Changes committed.")
                print("  Recovery: AuditLog WHERE Action='user.clean.test_email'")


if __name__ == "__main__":
    main()
