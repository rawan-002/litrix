"""
Quick verification that the Sprint 9 migration landed correctly.

Confirms:
  1. ResearchInterests column exists on the Researcher table.
  2. ResearchInterestsUpdatedAt column exists.
  3. The GIN index was created.
  4. Counts how many researchers already have interests populated
     (should be 0 right after migration).

USAGE:
    python verify_interests_column.py
"""
import os, sys
import django

if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "litrix_backend.settings"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()

from django.db import connection


def main():
    with connection.cursor() as cur:
        # 1. Columns
        cur.execute('''
            SELECT column_name, data_type, is_nullable
              FROM information_schema.columns
             WHERE table_name = 'Researcher'
               AND column_name IN ('ResearchInterests',
                                   'ResearchInterestsUpdatedAt')
             ORDER BY column_name
        ''')
        cols = cur.fetchall()
        print("Columns:")
        for name, dtype, nullable in cols:
            print(f"  - {name:32} {dtype:12} nullable={nullable}")
        if len(cols) != 2:
            print("  [!] Expected 2 columns, found", len(cols))

        # 2. Index
        cur.execute('''
            SELECT indexname, indexdef
              FROM pg_indexes
             WHERE tablename = 'Researcher'
               AND indexname = 'Researcher_ResearchInterests_gin_idx'
        ''')
        idx = cur.fetchone()
        print("\nIndex:")
        print(f"  - {idx[0]}\n      {idx[1]}" if idx else "  [!] GIN index missing")

        # 3. Population stats
        cur.execute('''
            SELECT
              COUNT(*)                                            AS total,
              COUNT(*) FILTER (
                WHERE jsonb_typeof("ResearchInterests") = 'array'
                  AND jsonb_array_length("ResearchInterests") > 0
              )                                                   AS populated,
              COUNT(*) FILTER (WHERE "ResearchInterestsUpdatedAt" IS NOT NULL)
                                                                  AS stamped
              FROM "Researcher"
        ''')
        total, populated, stamped = cur.fetchone()
        print(f"\nResearcher rows:")
        print(f"  - total            : {total}")
        print(f"  - with interests   : {populated}")
        print(f"  - stamped (synced) : {stamped}")
        if populated == 0:
            print("\n  [i] Next step: populate via")
            print("      python manage.py fetch_scholar_interests --limit 5 --dry-run")


if __name__ == "__main__":
    main()
