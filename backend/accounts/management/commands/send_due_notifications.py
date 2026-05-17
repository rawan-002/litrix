"""
Periodic worker — turns due ScheduledNotification rows into per-user
Notification rows that the inbox endpoint already knows how to render.

Usage:
    python manage.py send_due_notifications
    python manage.py send_due_notifications --dry-run   # report, don't write
    python manage.py send_due_notifications --limit 10  # process at most N rows

Deployment:
    Run this every 1–5 minutes via cron / Task Scheduler / Render Cron.
    The query uses FOR UPDATE SKIP LOCKED so two concurrent runs cannot
    double-deliver — whichever process gets a row first sends it.

TargetAudience JSON spec (any combination):
    {"all_researchers": true}      → every active Researcher in tenant
    {"user_type":   "Researcher"}  → every active user of that type
    {"role":        "HoD"}         → every user with that role NAME
    {"department_ids": [1, 2]}     → researchers in those departments
    {"user_ids":   [12, 34]}       → explicit user list (still filtered
                                     by tenant + IsActive)

If the resolver can't translate the spec, the row is marked FAILED with
the error logged in ErrorMessage — admins can fix and retry by editing
the row + flipping Status back to 'pending'.
"""
import json
import logging

from django.core.management.base import BaseCommand
from django.db import connection, transaction

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Send any ScheduledNotification rows whose SendAt has passed.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would be sent without writing.')
        parser.add_argument('--limit', type=int, default=50,
                            help='Max rows to process per invocation (default 50).')

    def handle(self, *args, **opts):
        dry_run = opts['dry_run']
        limit   = opts['limit']

        with transaction.atomic():
            with connection.cursor() as cur:
                # Claim a batch of due rows. SKIP LOCKED lets parallel
                # workers coexist without serializing.
                cur.execute(
                    '''SELECT "ScheduleID", "TenantID", "Title", "Body",
                              "NotificationType", "TargetAudience",
                              "RelatedCampaignID"
                       FROM "ScheduledNotification"
                       WHERE "Status" = 'pending'
                         AND "SendAt" <= NOW()
                       ORDER BY "SendAt" ASC
                       FOR UPDATE SKIP LOCKED
                       LIMIT %s''',
                    [limit],
                )
                rows = cur.fetchall()

                if not rows:
                    self.stdout.write('[i] No due notifications.')
                    return

                self.stdout.write(f'[>] Processing {len(rows)} due rows...')

                # In dry-run we just report and DON'T flip status. Rolling
                # back the transaction at the end keeps the rows pending.
                if not dry_run:
                    # Mark as 'processing' so other workers skip them
                    # even after we release the row lock at commit.
                    ids = [r[0] for r in rows]
                    cur.execute(
                        '''UPDATE "ScheduledNotification"
                           SET "Status" = 'processing'
                           WHERE "ScheduleID" = ANY(%s)''',
                        [ids],
                    )

                results = []
                for r in rows:
                    schedule_id, tenant_id, title, body, ntype, audience, campaign_id = r
                    try:
                        recipients = _resolve_audience(
                            cur, tenant_id, audience, campaign_id,
                        )
                        if dry_run:
                            results.append((schedule_id, len(recipients), None))
                            continue

                        if recipients:
                            _fan_out(cur, tenant_id, recipients, title, body,
                                     ntype, campaign_id)

                        cur.execute(
                            '''UPDATE "ScheduledNotification"
                               SET "Status" = 'sent',
                                   "SentAt" = NOW(),
                                   "RecipientCount" = %s
                               WHERE "ScheduleID" = %s''',
                            [len(recipients), schedule_id],
                        )
                        results.append((schedule_id, len(recipients), None))
                    except Exception as e:
                        logger.exception(
                            'send_due_notifications: row %s failed', schedule_id,
                        )
                        if not dry_run:
                            cur.execute(
                                '''UPDATE "ScheduledNotification"
                                   SET "Status" = 'failed',
                                       "ErrorMessage" = %s
                                   WHERE "ScheduleID" = %s''',
                                [str(e)[:1000], schedule_id],
                            )
                        results.append((schedule_id, 0, str(e)))

                if dry_run:
                    # Roll back everything in dry-run mode.
                    transaction.set_rollback(True)

        # Report outside the transaction
        for sid, count, err in results:
            if err:
                self.stdout.write(self.style.ERROR(
                    f'  [X] #{sid}: {err}'
                ))
            else:
                tag = '(dry-run) ' if dry_run else ''
                self.stdout.write(self.style.SUCCESS(
                    f'  [OK] {tag}#{sid} → {count} recipients'
                ))


# ----------------------------------------------------------------------
# Audience resolver
# ----------------------------------------------------------------------
def _resolve_audience(cur, tenant_id, audience, campaign_id=None):
    """
    Translate a TargetAudience JSON spec into a list of UserIDs.

    The spec is intentionally permissive — multiple keys can coexist
    (e.g. {"user_type": "Researcher", "department_ids": [3]} = active
    researchers in dept 3). Order of precedence:
        1. user_ids → explicit list, used as-is (still tenant-filtered)
        2. campaign_id → if RelatedCampaignID is set on the notification,
           we resolve to that campaign's exact submissions list.
           This ensures reminders go to the SAME people who got the
           original campaign-open notification.
        3. department_ids / department_id / user_type / role / all_researchers
    """
    if audience is None:
        audience = {}
    elif isinstance(audience, str):
        # In case Postgres returned a raw JSON string (driver edge case)
        try:
            audience = json.loads(audience)
        except Exception:
            audience = {}

    # ---- 1. Explicit user list ----------------------------------------
    explicit = audience.get('user_ids')
    if explicit:
        cur.execute(
            '''SELECT "UserID" FROM "Users"
               WHERE "TenantID" = %s AND "IsActive" = TRUE
                 AND "UserID" = ANY(%s)''',
            [tenant_id, explicit],
        )
        return [r[0] for r in cur.fetchall()]

    # ---- 2. Campaign-scoped (mirrors the audience that opened it) -----
    if campaign_id:
        cur.execute(
            '''SELECT s."UserID" FROM "ReportSubmission" s
               JOIN "Users" u ON u."UserID" = s."UserID"
               WHERE s."CampaignID" = %s
                 AND u."IsActive"   = TRUE
                 AND u."TenantID"   = %s''',
            [campaign_id, tenant_id],
        )
        return [r[0] for r in cur.fetchall()]

    # ---- 3. Generic filters -------------------------------------------
    where  = ['u."TenantID" = %s', 'u."IsActive" = TRUE']
    params = [tenant_id]

    if audience.get('all_researchers') or audience.get('user_type') == 'Researcher':
        where.append("u.\"UserType\" = 'Researcher'")
    elif audience.get('user_type'):
        where.append('u."UserType" = %s')
        params.append(audience['user_type'])

    if audience.get('role'):
        where.append('r."Name" = %s')
        params.append(audience['role'])

    dept_ids = (audience.get('department_ids')
                or ([audience['department_id']] if audience.get('department_id') else None))
    if dept_ids:
        where.append('w."DepartmentID" = ANY(%s)')
        params.append(dept_ids)

    cur.execute(
        f'''SELECT DISTINCT u."UserID"
            FROM "Users" u
            LEFT JOIN "Role"     r ON r."RoleID"     = u."RoleID"
            LEFT JOIN "Works_In" w ON w."UserID"     = u."UserID"
                                  AND w."IsCurrentPosition" = TRUE
            WHERE {' AND '.join(where)}''',
        params,
    )
    return [r[0] for r in cur.fetchall()]


# ----------------------------------------------------------------------
# Fan-out
# ----------------------------------------------------------------------
def _fan_out(cur, tenant_id, user_ids, title, body, ntype, campaign_id):
    """
    Bulk-insert one Notification row per recipient.

    We chunk into 500-row INSERTs to keep parameter counts reasonable
    when broadcasting to large audiences (e.g. all researchers).
    """
    metadata = json.dumps({
        'campaign_id': campaign_id,
        'source':      'scheduled_notification',
    })
    CHUNK = 500

    for i in range(0, len(user_ids), CHUNK):
        chunk = user_ids[i:i + CHUNK]
        # Build a VALUES clause with placeholders
        placeholders = ','.join(
            ['(%s, %s, %s, %s, %s, %s::jsonb)'] * len(chunk)
        )
        params = []
        for uid in chunk:
            params.extend([tenant_id, uid, ntype, title, body, metadata])

        cur.execute(
            f'''INSERT INTO "Notification"
                   ("TenantID", "UserID", "Type", "Title", "Message", "Metadata")
                VALUES {placeholders}''',
            params,
        )
