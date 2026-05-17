"""
Delete the 92 Manual orphan papers.

Same pattern as delete_openalex_orphans.py:
  - Filter by Source='Manual' AND no Authors row
  - AuditLog snapshot per paper (Action='paper.delete.manual_orphan')
  - Cascade-delete child rows from tables that actually exist
  - Single transaction, --dry-run supported

USAGE:
    python delete_manual_orphans.py --dry-run
    python delete_manual_orphans.py
"""
import os, sys, json, argparse
import django

if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "litrix_backend.settings"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()

from django.db import connection, transaction


POSSIBLE_CHILDREN = [
    "ExternalAuthors", "PaperKeywords", "PaperGrants",
    "Citations", "CitationsHistory", "ReportPaperDecision",
]

TARGET_SQL = """
SELECT rp."PaperID", rp."Title", rp."PubYear", rp."DOI", rp."Source"
FROM "ResearchPaper" rp
WHERE rp."Source" = 'Manual'
  AND NOT EXISTS (
      SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID"
  )
ORDER BY rp."PaperID"
"""


def existing_child_tables(cur):
    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = ANY(%s)
    """, [POSSIBLE_CHILDREN])
    return [r[0] for r in cur.fetchall()]


def child_counts(cur, paper_id, tables):
    counts = {}
    for table in tables:
        cur.execute(f'SELECT COUNT(*) FROM "{table}" WHERE "PaperID" = %s',
                    (paper_id,))
        counts[table] = cur.fetchone()[0]
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with transaction.atomic():
        with connection.cursor() as cur:
            tables = existing_child_tables(cur)
            print("Child tables present:", tables)
            missing = sorted(set(POSSIBLE_CHILDREN) - set(tables))
            if missing:
                print("  (Will skip - do not exist:", missing, ")")

            cur.execute(TARGET_SQL)
            targets = cur.fetchall()
            print(f"\nManual orphans to delete: {len(targets)}")
            if not targets:
                print("Nothing to do.")
                return

            print("\nSample (first 3):")
            for t in targets[:3]:
                print(f"  PaperID={t[0]:<6} year={t[2]}  doi={t[3]}")
                print(f"    title: {(t[1] or '')[:90]}")

            n_deleted = 0
            for paper_id, title, pub_year, doi, source in targets:
                counts = child_counts(cur, paper_id, tables)
                snapshot = {
                    "PaperID":  paper_id,
                    "Title":    title,
                    "PubYear":  pub_year,
                    "DOI":      doi,
                    "Source":   source,
                    "ChildCountsBefore": counts,
                    "Reason":   "manual_orphan_not_college_of_computing",
                }
                cur.execute(
                    'INSERT INTO "AuditLog" '
                    '("TenantID","UserID","Action","TargetType","TargetID",'
                    ' "Metadata","IpAddress","UserAgent") '
                    'VALUES (1, NULL, %s, %s, %s, %s::jsonb, NULL, %s)',
                    ["paper.delete.manual_orphan", "ResearchPaper", paper_id,
                     json.dumps(snapshot),
                     "backfill_script:delete_manual_orphans"]
                )
                for t in tables:
                    cur.execute(f'DELETE FROM "{t}" WHERE "PaperID" = %s',
                                (paper_id,))
                cur.execute('DELETE FROM "ResearchPaper" WHERE "PaperID" = %s',
                            (paper_id,))
                n_deleted += 1
                if n_deleted % 25 == 0:
                    print(f"  ... {n_deleted} processed")

            cur.execute(
                'SELECT COUNT(*) FROM "ResearchPaper" rp '
                'WHERE NOT EXISTS ('
                '  SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID")'
            )
            remaining = cur.fetchone()[0]

            print(f"\nDeleted: {n_deleted}")
            print(f"Remaining orphan papers (all sources): {remaining}")

            if args.dry_run:
                transaction.set_rollback(True)
                print("\n--dry-run: rolled back. Nothing written.")
            else:
                print("\nDone. Changes committed.")
                print("Recovery: AuditLog WHERE Action='paper.delete.manual_orphan'")


if __name__ == "__main__":
    main()
