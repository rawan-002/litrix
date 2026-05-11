"""
One-shot runner for the Litrix-ID migration.

Why this script?
    • Avoids PowerShell's `<` redirection quoting issue with psql.
    • Uses Django's existing DB connection — no need to remember the
      host/user/password; reads them straight from settings.py / .env.
    • Idempotent — the SQL itself is safe to re-run.

Usage (from backend/ folder):
    python scripts/migrations/run_sprint3_migration.py
"""
import os
import sys
import django
from pathlib import Path

# Ensure backend/ is on sys.path so Django can find the project package
# regardless of where this script is invoked from. parents[2] resolves
# to backend/ because we live at backend/scripts/migrations/<file>.py.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_BACKEND_DIR))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'litrix_backend.settings')
django.setup()


def main():
    sql_path = _BACKEND_DIR / 'analytics' / 'migrations' / 'sprint3_litrix_id.sql'
    if not sql_path.exists():
        print(f'[X] SQL file not found: {sql_path}')
        sys.exit(1)

    sql = sql_path.read_text(encoding='utf-8')

    from django.db import connection
    with connection.cursor() as cur:
        print(f'[>] Running {sql_path.name}...')
        cur.execute(sql)
    print('[OK] Migration applied successfully.')

    # Quick sanity check — show how many users now have a Litrix_ID.
    with connection.cursor() as cur:
        cur.execute('''
            SELECT
                COUNT(*) FILTER (WHERE "Litrix_ID" IS NOT NULL) AS assigned,
                COUNT(*)                                          AS total,
                MIN("Litrix_ID")                                  AS first_id,
                MAX("Litrix_ID")                                  AS last_id
              FROM "Users"
        ''')
        assigned, total, first_id, last_id = cur.fetchone()
        print(f'[i] {assigned}/{total} users have a Litrix_ID')
        if assigned:
            print(f'[i] Range: {first_id}  ...  {last_id}')


if __name__ == '__main__':
    main()
