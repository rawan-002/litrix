"""
DEPRECATED — replaced by a Django management command.

Two options now:
    1. Management command (CLI):
         python manage.py delete_user --user-id 42 --yes
         python manage.py delete_user --email a@b.com --yes

    2. Admin API endpoint (from the web UI):
         DELETE /api/auth/users/<user_id>/
       — fires the same cascade with audit logging tied to the
       requesting admin.

The new command file lives at:
    backend/accounts/management/commands/delete_user.py
"""
raise SystemExit(
    'delete_user.py has moved. Use:  python manage.py delete_user --user-id <id> --yes'
)
