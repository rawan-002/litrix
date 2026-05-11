"""
Bootstrap (or re-bootstrap) the system Admin account.

Usage:
    python manage.py create_admin
    python manage.py create_admin --email admin@litrix.com --password 'NewP@ss!' --name "Litrix Admin"

Why a management command (not a standalone script)?
    • Django loads settings, app registry, and DB connection for you —
      no `os.environ.setdefault(...) + django.setup()` boilerplate.
    • Discoverable: appears in `python manage.py help` listing.
    • Testable: importable from tests via call_command('create_admin').
    • Path-independent: works from any cwd, no fragile relative imports.

Idempotency:
    • If a user with the same email exists, we UPDATE its password,
      promote it to Admin role / UserType=Admin, and reactivate it.
    • If no user exists, we INSERT a fresh row with the bound RoleID.
    Either way the command finishes with a single Admin row guaranteed
    to be active, email-verified, and password-protected.

Why raw SQL on the Users table?
    The Users table is `managed = False` (the scraper owns the schema).
    Going through the ORM still works for SELECT, but the INSERT path
    relies on column-level defaults set by SQL triggers (Litrix_ID,
    CreatedAt, etc.). Raw SQL keeps the canonical insert path identical
    to the production registration / approval flow.
"""
from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction


DEFAULT_EMAIL    = 'admin@litrix.com'
DEFAULT_PASSWORD = 'Admin123!Litrix'
DEFAULT_NAME     = 'Litrix Admin'
DEFAULT_TENANT   = 1


class Command(BaseCommand):
    help = 'Create or reset the system Admin user (idempotent).'

    def add_arguments(self, parser):
        parser.add_argument('--email',    default=DEFAULT_EMAIL)
        parser.add_argument('--password', default=DEFAULT_PASSWORD)
        parser.add_argument('--name',     default=DEFAULT_NAME,
                            help='Stored as FullName_Ar.')
        parser.add_argument('--tenant',   type=int, default=DEFAULT_TENANT)

    def handle(self, *args, **opts):
        email    = opts['email'].lower().strip()
        password = opts['password']
        name_ar  = opts['name']
        tenant   = opts['tenant']

        with transaction.atomic():
            with connection.cursor() as cur:
                # Resolve the Admin RoleID for this tenant. The Role table
                # is seeded by sprint1_foundation.sql — if it's empty,
                # something upstream went wrong and we should hard-fail
                # rather than create an Admin user with no role.
                cur.execute(
                    'SELECT "RoleID" FROM "Role" '
                    'WHERE "Name" = %s AND "TenantID" = %s',
                    ['Admin', tenant],
                )
                row = cur.fetchone()
                if not row:
                    raise CommandError(
                        f'No "Admin" role found for tenant {tenant}. '
                        f'Run the sprint1_foundation.sql migration first.'
                    )
                role_id = row[0]

                # Does this email already exist?
                cur.execute(
                    'SELECT "UserID" FROM "Users" '
                    'WHERE LOWER("Email") = LOWER(%s)',
                    [email],
                )
                existing = cur.fetchone()
                password_hash = make_password(password)

                if existing:
                    user_id = existing[0]
                    cur.execute(
                        '''
                        UPDATE "Users" SET
                            "PasswordHash"  = %s,
                            "FullName_Ar"   = COALESCE(%s, "FullName_Ar"),
                            "UserType"      = 'Admin',
                            "AccountStatus" = 'Active',
                            "RoleID"        = %s,
                            "TenantID"      = %s,
                            "EmailVerified" = TRUE,
                            "IsActive"      = TRUE
                        WHERE "UserID" = %s
                        ''',
                        [password_hash, name_ar, role_id, tenant, user_id],
                    )
                    action = 'updated'
                else:
                    cur.execute(
                        '''
                        INSERT INTO "Users"
                            ("Email", "PasswordHash", "FullName_Ar",
                             "UserType", "AccountStatus", "TenantID",
                             "RoleID", "EmailVerified", "IsActive",
                             "CreatedAt")
                        VALUES (%s, %s, %s, 'Admin', 'Active', %s,
                                %s, TRUE, TRUE, NOW())
                        RETURNING "UserID"
                        ''',
                        [email, password_hash, name_ar, tenant, role_id],
                    )
                    user_id = cur.fetchone()[0]
                    action = 'created'

        self.stdout.write(self.style.SUCCESS(
            f'[OK] Admin {action}: UserID={user_id}  Email={email}'
        ))
        self.stdout.write(self.style.WARNING(
            '[!]  Change the default password immediately if you used the default.'
        ))
