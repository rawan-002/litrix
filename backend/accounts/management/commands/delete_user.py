"""
CLI counterpart of the DELETE /api/auth/users/<id>/ endpoint.

Usage:
    python manage.py delete_user --user-id 42
    python manage.py delete_user --email someone@example.com --yes

Why a CLI when the API already supports delete?
    • Emergency cleanup when no Admin can log in.
    • Scripted bulk deletion via shell loops.
    • The API DELETE path enforces a "can't delete yourself" guard tied
      to the requesting user — this command has no such concept, but it
      DOES require an explicit --yes flag to prevent muscle-memory typos.

Behaviour mirrors `accounts.views._delete_user` exactly so the audit
trail (Department.HeadID, Audit.UserID, etc.) is preserved the same
way regardless of which path triggered the delete.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction


class Command(BaseCommand):
    help = 'Hard-delete a user and all their owned rows (papers, sync jobs, etc.).'

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--user-id', type=int, help='Internal UserID')
        group.add_argument('--email',   type=str, help='Email address')
        parser.add_argument(
            '--yes', action='store_true',
            help='Confirm the deletion (required — no prompt is shown).',
        )

    def _table_exists(self, cur, name: str) -> bool:
        cur.execute(
            'SELECT 1 FROM information_schema.tables '
            'WHERE table_schema = current_schema() AND table_name = %s',
            [name],
        )
        return cur.fetchone() is not None

    def handle(self, *args, **opts):
        if not opts['yes']:
            raise CommandError(
                'Refusing to delete without --yes. '
                'Re-run with --yes to confirm.'
            )

        with connection.cursor() as cur:
            if opts['user_id']:
                cur.execute(
                    'SELECT "UserID", "Email", "FullName_Ar", "TenantID" '
                    'FROM "Users" WHERE "UserID" = %s',
                    [opts['user_id']],
                )
            else:
                cur.execute(
                    'SELECT "UserID", "Email", "FullName_Ar", "TenantID" '
                    'FROM "Users" WHERE LOWER("Email") = LOWER(%s)',
                    [opts['email']],
                )
            row = cur.fetchone()
            if not row:
                raise CommandError('User not found.')
            user_id, email, name_ar, tenant_id = row

        self.stdout.write(
            f'[i] Deleting UserID={user_id}  Email={email}  Name={name_ar}'
        )

        with transaction.atomic():
            with connection.cursor() as cur:
                # Phase 1: null out FKs to keep history readable
                cur.execute(
                    'UPDATE "Department" SET "HeadID" = NULL WHERE "HeadID" = %s',
                    [user_id])
                cur.execute(
                    'UPDATE "RegistrationRequest" SET "ReviewedByUserID" = NULL '
                    'WHERE "ReviewedByUserID" = %s', [user_id])
                cur.execute(
                    'UPDATE "AuditLog" SET "UserID" = NULL WHERE "UserID" = %s',
                    [user_id])
                cur.execute(
                    'UPDATE "SyncJob" SET "TriggeredBy" = NULL '
                    'WHERE "TriggeredBy" = %s', [user_id])

                if self._table_exists(cur, 'Invitation'):
                    cur.execute(
                        'UPDATE "Invitation" SET "InvitedByUserID" = NULL '
                        'WHERE "InvitedByUserID" = %s', [user_id])
                    cur.execute(
                        'UPDATE "Invitation" SET "UsedByUserID" = NULL '
                        'WHERE "UsedByUserID" = %s', [user_id])

                # SimpleJWT outstanding tokens hold FK on Users
                if self._table_exists(cur, 'token_blacklist_blacklistedtoken') \
                        and self._table_exists(cur, 'token_blacklist_outstandingtoken'):
                    cur.execute('''
                        DELETE FROM token_blacklist_blacklistedtoken
                        WHERE token_id IN (
                            SELECT id FROM token_blacklist_outstandingtoken
                            WHERE user_id = %s
                        )
                    ''', [user_id])
                if self._table_exists(cur, 'token_blacklist_outstandingtoken'):
                    cur.execute(
                        'DELETE FROM token_blacklist_outstandingtoken '
                        'WHERE user_id = %s', [user_id])

                # Phase 2: user-owned rows
                cur.execute('DELETE FROM "SyncJob"      WHERE "UserID" = %s', [user_id])
                cur.execute('DELETE FROM "Notification" WHERE "UserID" = %s', [user_id])
                cur.execute('DELETE FROM "Authors"      WHERE "UserID" = %s', [user_id])
                cur.execute('DELETE FROM "Works_In"     WHERE "UserID" = %s', [user_id])
                cur.execute('DELETE FROM "Researcher"   WHERE "UserID" = %s', [user_id])

                # Phase 3: the user row
                cur.execute('DELETE FROM "Users" WHERE "UserID" = %s', [user_id])

        self.stdout.write(self.style.SUCCESS(
            f'[OK] User {user_id} ({email}) deleted.'
        ))
