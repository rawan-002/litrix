"""
One-shot runner for the empty-ID normalization migration.

Usage (from backend/ folder):
    python scripts/migrations/run_sprint5_migration.py
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
    sql_path = (
        _BACKEND_DIR / 'analytics' / 'migrations' / 'sprint5_normalize_empty_ids.sql'
    )
    if not sql_path.exists():
        print(f'[X] SQL file not found: {sql_path}')
        sys.exit(1)

    sql = sql_path.read_text(encoding='utf-8')

    from django.db import connection

    # Snapshot before so we can show the impact.
    with connection.cursor() as cur:
        cur.execute('''
            SELECT
                COUNT(*) FILTER (WHERE "Scholar_ID" = '') AS sch,
                COUNT(*) FILTER (WHERE "Orcid_ID"   = '') AS orc,
                COUNT(*) FILTER (WHERE "Scopus_ID"  = '') AS sco
              FROM "Users"
        ''')
        before = cur.fetchone()
    print(f'[i] Empty before: scholar={before[0]}  orcid={before[1]}  scopus={before[2]}')

    with connection.cursor() as cur:
        print(f'[>] Running {sql_path.name}...')
        cur.execute(sql)
    print('[OK] Normalization applied.')

    with connection.cursor() as cur:
        cur.execute('''
            SELECT
                COUNT(*) FILTER (WHERE "Scholar_ID" = '') AS sch,
                COUNT(*) FILTER (WHERE "Orcid_ID"   = '') AS orc,
                COUNT(*) FILTER (WHERE "Scopus_ID"  = '') AS sco
              FROM "Users"
        ''')
        after = cur.fetchone()
    print(f'[i] Empty after:  scholar={after[0]}  orcid={after[1]}  scopus={after[2]}')


if __name__ == '__main__':
    main()
