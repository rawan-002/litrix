"""
One-shot runner for Sprint 7: reporting campaigns + scheduled notifications.

What it does:
    Applies analytics/migrations/sprint7_reports.sql via Django's DB
    connection, then prints a sanity snapshot of the new tables.

Usage (from backend/ folder):
    python scripts/migrations/run_sprint7_migration.py

The SQL itself is idempotent (IF NOT EXISTS / ON CONFLICT throughout),
so re-running is safe.
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
    sql_path = _BACKEND_DIR / 'analytics' / 'migrations' / 'sprint7_reports.sql'
    if not sql_path.exists():
        print(f'[X] SQL file not found: {sql_path}')
        sys.exit(1)

    sql = sql_path.read_text(encoding='utf-8')

    from django.db import connection, transaction

    print(f'[>] Running {sql_path.name}...')

    # Wrap the whole DDL in a single transaction so a failure mid-file
    # rolls back cleanly — no half-created tables on the next run.
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(sql)
    print('[OK] Migration applied successfully.')

    # Sanity snapshot — confirms each table exists + reports row counts.
    print('\n[i] Table snapshot:')
    with connection.cursor() as cur:
        for tbl in (
            'ReportCampaign',
            'ReportSubmission',
            'ReportPaperDecision',
            'ScheduledNotification',
        ):
            cur.execute(f'SELECT COUNT(*) FROM "{tbl}"')
            n = cur.fetchone()[0]
            print(f'    {tbl:<24} → {n} rows')

        # Confirm the new permissions registered
        cur.execute('''
            SELECT "Code"
            FROM "Permission"
            WHERE "Code" IN (
                'manage_campaigns',
                'view_campaign_reports',
                'compose_notifications'
            )
            ORDER BY "Code"
        ''')
        perms = [r[0] for r in cur.fetchall()]
        print(f'\n[i] Permissions added: {perms}')

        # And confirm Admin role got them
        cur.execute('''
            SELECT p."Code"
            FROM "RolePermission" rp
            JOIN "Role"       r ON r."RoleID"       = rp."RoleID"
            JOIN "Permission" p ON p."PermissionID" = rp."PermissionID"
            WHERE r."Name" = 'Admin' AND r."TenantID" = 1
              AND p."Code" IN (
                  'manage_campaigns',
                  'view_campaign_reports',
                  'compose_notifications'
              )
            ORDER BY p."Code"
        ''')
        granted = [r[0] for r in cur.fetchall()]
        print(f'[i] Granted to Admin:  {granted}')


if __name__ == '__main__':
    main()
