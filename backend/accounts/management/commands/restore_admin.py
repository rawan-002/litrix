"""
Emergency break-glass: reactivate the Admin account when it's been
accidentally locked out, soft-deleted, or had its IsActive flag flipped.

Usage:
    python manage.py restore_admin
    python manage.py restore_admin --email someone@example.com
    python manage.py restore_admin --reset-password 'NewP@ss!'

What it does:
    • Locates the user by email (defaults to admin@litrix.com).
    • Forces IsActive=TRUE, EmailVerified=TRUE, AccountStatus='Active'.
    • Re-attaches the canonical Admin RoleID for the tenant.
    • Optionally resets the password (only when --reset-password given).

Why separate from create_admin?
    create_admin is the bootstrap path — it provisions a fresh Admin if
    none exists. restore_admin is the recovery path — it assumes the
    Admin row exists but is unusable. Keeping them distinct makes the
    intent obvious in audit logs and avoids accidentally CREATING a new
    Admin when you meant to FIX an old one.
"""
from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction


DEFAULT_EMAIL  = 'admin@litrix.com'
DEFAULT_TENANT = 1


class Command(BaseCommand):
    help = 'Reactivate a locked-out Admin account (break-glass tool).'

    def add_arguments(self, parser):
        parser.add_argument('--email', default=DEFAULT_EMAIL)
        parser.add_argument('--tenant', type=int, default=DEFAULT_TENANT)
        parser.add_argument(
            '--reset-password', default=None,
            help='If provided, the account password is also reset.',
        )

    def handle(self, *args, **opts):
        email    = opts['email'].lower().strip()
        tenant   = opts['tenant']
        new_pwd  = opts['reset_password']

        with transaction.atomic():
            with connection.cursor() as cur:
                # Find the user
                cur.execute(
                    'SELECT "UserID", "UserType", "AccountStatus", "IsActive", '
                    '"EmailVerified", "RoleID" '
                    'FROM "Users" WHERE LOWER("Email") = LOWER(%s) AND "TenantID" = %s',
                    [email, tenant],
                )
                row = cur.fetchone()
                if not row:
                    raise CommandError(
                        f'No user with email {email} (tenant {tenant}). '
                        f'Use `python manage.py create_admin` to bootstrap a new one.'
                    )
                user_id, user_type, status_v, is_active, email_verified, role_id = row

                self.stdout.write(
                    f'[i] Found UserID={user_id}  '
                    f'UserType={user_type}  Status={status_v}  '
                    f'IsActive={is_active}  EmailVerified={email_verified}'
                )

                # Resolve canonical Admin role for the tenant
                cur.execute(
                    'SELECT "RoleID" FROM "Role" '
                    'WHERE "Name" = %s AND "TenantID" = %s',
                    ['Admin', tenant],
                )
                rrow = cur.fetchone()
                if not rrow:
                    raise CommandError(
                        f'No "Admin" role configured for tenant {tenant}.'
                    )
                admin_role_id = rrow[0]

                # Build the UPDATE
                params = [admin_role_id, user_id, tenant]
                pwd_clause = ''
                if new_pwd:
                    pwd_clause = ', "PasswordHash" = %s'
                    params.insert(0, make_password(new_pwd))

                cur.execute(
                    f'''
                    UPDATE "Users" SET
                        "UserType"      = 'Admin',
                        "AccountStatus" = 'Active',
                        "IsActive"      = TRUE,
                        "EmailVerified" = TRUE,
                        "RoleID"        = %s
                        {pwd_clause}
                    WHERE "UserID" = %s AND "TenantID" = %s
                    ''',
                    params,
                )

        self.stdout.write(self.style.SUCCESS(
            f'[OK] Admin restored: UserID={user_id}  Email={email}'
            + ('  (password reset)' if new_pwd else '')
        ))
