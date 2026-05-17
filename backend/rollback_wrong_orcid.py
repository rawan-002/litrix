"""
Rollback wrongly-attributed papers caused by an incorrect ORCID
in the Users table.

CONTEXT
-------
A researcher's Orcid_ID was set to a value that doesn't actually belong
to them. The ORCID-based backfill (PASS 1 of fix_orphan_papers_v2.py)
then linked many papers to them in good faith - those links are wrong.

This script:
  1. Confirms which user has the disputed ORCID.
  2. Deletes all Authors rows for that user with
     MappingCriteria = 'orcid_backfill'.
  3. Optionally clears the Orcid_ID from the Users row.
  4. Writes an AuditLog entry summarizing the rollback.

EDIT THESE TWO CONSTANTS BEFORE RUNNING.

USAGE:
    python rollback_wrong_orcid.py --dry-run
    python rollback_wrong_orcid.py
"""
import os, sys, json, argparse
import django

if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "litrix_backend.settings"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()

from django.db import connection, transaction


# ============================================================================
# EDIT THESE BEFORE RUNNING
# ============================================================================
USER_ID         = 81                    # محمد ابراهيم آل مشلح
WRONG_ORCID     = "0000-0002-9794-554X" # The ORCID that doesn't belong to him
CLEAR_ORCID     = True                  # also wipe his Orcid_ID column
# ============================================================================


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with transaction.atomic():
        with connection.cursor() as cur:

            # 1. Confirm identity
            cur.execute("""
                SELECT "UserID", "FullName_Ar", "Scholar_ID", "Orcid_ID"
                FROM "Users" WHERE "UserID" = %s
            """, [USER_ID])
            row = cur.fetchone()
            if not row:
                print(f"UserID={USER_ID} not found.")
                return
            print(f"User: {row[1]}")
            print(f"  Scholar: {row[2]}")
            print(f"  ORCID  : {row[3]}")
            if row[3] != WRONG_ORCID:
                print(f"Note: stored ORCID is {row[3]}, expected {WRONG_ORCID}.")

            # 2. Count what we're about to delete
            cur.execute("""
                SELECT COUNT(*) FROM "Authors"
                WHERE "UserID" = %s
                  AND "MappingCriteria" = 'orcid_backfill'
            """, [USER_ID])
            n_to_delete = cur.fetchone()[0]
            print(f"\nAuthors rows to delete (orcid_backfill for this user): {n_to_delete}")

            # 3. Sample of what we're deleting
            cur.execute("""
                SELECT rp."PaperID", LEFT(rp."Title", 80), a."AuthorNameRaw"
                FROM "Authors" a
                JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
                WHERE a."UserID" = %s
                  AND a."MappingCriteria" = 'orcid_backfill'
                ORDER BY rp."PaperID"
                LIMIT 5
            """, [USER_ID])
            print("\nSample (first 5):")
            for r in cur.fetchall():
                print(f"  PaperID={r[0]}  scraped_name={r[2]}")
                print(f"    title: {r[1]}")

            # 4. Delete them
            cur.execute("""
                DELETE FROM "Authors"
                WHERE "UserID" = %s
                  AND "MappingCriteria" = 'orcid_backfill'
            """, [USER_ID])
            n_deleted = cur.rowcount
            print(f"\nDeleted {n_deleted} Authors rows.")

            # 5. Clear the wrong ORCID from BOTH possible locations
            #    (Users.Orcid_ID and Researcher.ORCID_ID)
            if CLEAR_ORCID:
                cur.execute("""
                    UPDATE "Users" SET "Orcid_ID" = NULL WHERE "UserID" = %s
                """, [USER_ID])
                u_cleared = cur.rowcount
                cur.execute("""
                    UPDATE "Researcher" SET "ORCID_ID" = NULL WHERE "UserID" = %s
                """, [USER_ID])
                r_cleared = cur.rowcount
                print(f"Cleared ORCID: Users.Orcid_ID={u_cleared}, "
                      f"Researcher.ORCID_ID={r_cleared}")

            # 6. Audit
            cur.execute(
                'INSERT INTO "AuditLog" '
                '("TenantID","UserID","Action","TargetType","TargetID",'
                ' "Metadata","IpAddress","UserAgent") '
                'VALUES (1, NULL, %s, %s, %s, %s::jsonb, NULL, %s)',
                ["author.unlink.wrong_orcid", "Users", USER_ID,
                 json.dumps({
                     "user_id":     USER_ID,
                     "wrong_orcid": WRONG_ORCID,
                     "n_deleted":   n_deleted,
                     "cleared_orcid": CLEAR_ORCID,
                 }),
                 "rollback_script:rollback_wrong_orcid"]
            )

            if args.dry_run:
                transaction.set_rollback(True)
                print("\n--dry-run: rolled back. Nothing persisted.")
            else:
                print("\nDone. Changes committed.")
                print("Recovery: AuditLog WHERE Action='author.unlink.wrong_orcid'")


if __name__ == "__main__":
    main()
