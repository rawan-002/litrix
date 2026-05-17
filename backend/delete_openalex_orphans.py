"""
Delete OpenAlex orphan papers - papers from Source='OpenAlex' that have
NO Authors row pointing to any registered researcher.

SAFETY
------
1. Snapshot every deleted paper into AuditLog.Metadata BEFORE removing it.
2. Defensive cleanup: only DELETE from child tables that actually exist
   in THIS database (some tables in the schema PDF aren't deployed).
3. Single transaction. --dry-run rolls back at the end.

USAGE
-----
    python delete_openalex_orphans.py --dry-run
    python delete_openalex_orphans.py
"""
import os, sys, json, argparse
import django

if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "litrix_backend.settings"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()

from django.db import connection, transaction


# All tables that might reference ResearchPaper.PaperID. We probe each
# one and keep only those that actually exist in this DB.
POSSIBLE_CHILD_TABLES = [
    "ExternalAuthors",
    "PaperKeywords",
    "PaperGrants",
    "Citations",
    "CitationsHistory",
    "ReportPaperDecision",   # Sprint 7
]


TARGET_SQL = """
SELECT rp."PaperID", rp."Title", rp."PubYear", rp."DOI", rp."Source"
FROM "ResearchPaper" rp
WHERE rp."Source" = 'OpenAlex'
  AND NOT EXISTS (
      SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID"
  )
ORDER BY rp."PaperID"
"""


def existing_child_tables(cur):
    """Return only the child tables that actually exist in the DB."""
    cur.execute("""
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = ANY(%s)
    """, [POSSIBLE_CHILD_TABLES])
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
            # Discover which child tables exist
            tables = existing_child_tables(cur)
            print("Child tables present in this DB:", tables)
            missing = sorted(set(POSSIBLE_CHILD_TABLES) - set(tables))
            if missing:
                print("  (Will skip - do not exist:", missing, ")")

            # 1. Identify the candidates
            cur.execute(TARGET_SQL)
            targets = cur.fetchall()
            print(f"\nOpenAlex orphans to delete: {len(targets)}")
            if not targets:
                print("Nothing to do.")
                return

            print("\nSample (first 3):")
            for t in targets[:3]:
                print(f"  PaperID={t[0]:<6} year={t[2]}  doi={t[3]}")
                print(f"    title: {(t[1] or '')[:90]}")

            # 2. Per-paper: snapshot, then cascade delete
            n_deleted = 0
            for paper_id, title, pub_year, doi, source in targets:
                counts = child_counts(cur, paper_id, tables)
                snapshot = {
                    "PaperID":          paper_id,
                    "Title":            title,
                    "PubYear":          pub_year,
                    "DOI":              doi,
                    "Source":           source,
                    "ChildCountsBefore": counts,
                    "Reason":           "openalex_orphan_no_albaha_author",
                }
                cur.execute(
                    'INSERT INTO "AuditLog" '
                    '("TenantID","UserID","Action","TargetType","TargetID",'
                    ' "Metadata","IpAddress","UserAgent") '
                    'VALUES (%s, NULL, %s, %s, %s, %s::jsonb, NULL, %s)',
                    [1, "paper.delete.orphan", "ResearchPaper", paper_id,
                     json.dumps(snapshot),
                     "backfill_script:delete_openalex_orphans"]
                )

                for table in tables:
                    cur.execute(f'DELETE FROM "{table}" WHERE "PaperID" = %s',
                                (paper_id,))
                cur.execute('DELETE FROM "ResearchPaper" WHERE "PaperID" = %s',
                            (paper_id,))
                n_deleted += 1

                if n_deleted % 50 == 0:
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
                print("Recovery: AuditLog WHERE Action='paper.delete.orphan' "
                      "(Metadata has the snapshot).")


if __name__ == "__main__":
    main()
