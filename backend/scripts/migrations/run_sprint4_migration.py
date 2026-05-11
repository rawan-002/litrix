"""
One-shot runner for the Litrix-ID normalization migration.

What it does:
    Rewrites any LIT-XXXXXX / lit-N / Lit-X variants to the canonical
    Lit-NNNNNN form (capital L only, 6 zero-padded digits).

Usage (from backend/ folder):
    python scripts/migrations/run_sprint4_migration.py
"""
import os
import sys
import django
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_BACKEND_DIR))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'litrix_backend.settings')
django.setup()


def main():
    sql_path = _BACKEND_DIR / 'analytics' / 'migrations' / 'sprint4_normalize_litrix_id.sql'
    if not sql_path.exists():
        print(f'[X] SQL file not found: {sql_path}')
        sys.exit(1)

    sql = sql_path.read_text(encoding='utf-8')

    from django.db import connection

    # Sanity snapshot: how many non-canonical rows do we have?
    with connection.cursor() as cur:
        cur.execute('''
            SELECT COUNT(*)
              FROM "Users"
             WHERE "Litrix_ID" ~* '^lit-[0-9]+$'
               AND "Litrix_ID" !~ '^Lit-[0-9]{6}$'
        ''')
        before = cur.fetchone()[0]
    print(f'[i] {before} rows are non-canonical before the run')

    with connection.cursor() as cur:
        print(f'[>] Running {sql_path.name}...')
        cur.execute(sql)
    print('[OK] Normalization applied successfully.')

    # Verify the after state.
    with connection.cursor() as cur:
        cur.execute('''
            SELECT
                COUNT(*) FILTER (WHERE "Litrix_ID" ~ '^Lit-[0-9]{6}$') AS canonical,
                COUNT(*) FILTER (WHERE "Litrix_ID" IS NOT NULL)        AS total,
                MIN(CAST(SUBSTRING("Litrix_ID" FROM 5) AS INTEGER))    AS min_seq,
                MAX(CAST(SUBSTRING("Litrix_ID" FROM 5) AS INTEGER))    AS max_seq
              FROM "Users"
             WHERE "Litrix_ID" ~* '^lit-[0-9]+$'
        ''')
        canonical, total, min_seq, max_seq = cur.fetchone()
    print(f'[i] {canonical}/{total} rows are now canonical')
    if min_seq is not None:
        print(f'[i] Sequence range: Lit-{min_seq:06d}  ...  Lit-{max_seq:06d}')


if __name__ == '__main__':
    main()
