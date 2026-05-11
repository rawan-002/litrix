"""Apply the Invitation-table migration via Django's DB connection.

Usage (from backend/ folder):
    python scripts/migrations/run_sprint6_migration.py
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
    sql_path = _BACKEND_DIR / 'analytics' / 'migrations' / 'sprint6_invitations.sql'
    if not sql_path.exists():
        print(f'[X] {sql_path} not found')
        sys.exit(1)

    from django.db import connection
    with connection.cursor() as cur:
        print(f'[>] Running {sql_path.name}...')
        cur.execute(sql_path.read_text(encoding='utf-8'))
    print('[OK] Migration applied.')


if __name__ == '__main__':
    main()
