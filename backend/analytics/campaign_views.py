"""
Admin-facing endpoints for the Reporting Campaigns workflow.

Endpoint map:
    GET    /api/campaigns/                       — list (filtered by perm)
    POST   /api/campaigns/                       — create draft
    GET    /api/campaigns/<id>/                  — detail + submission stats
    PATCH  /api/campaigns/<id>/                  — edit draft only
    POST   /api/campaigns/<id>/open/             — draft → active (fan-out)
    POST   /api/campaigns/<id>/close/            — active → closed
    GET    /api/campaigns/<id>/submissions/      — list submissions

Key design decisions:
    • State transitions are HTTP-action endpoints, not PATCH on the
      status field. This makes the workflow explicit + lets each
      transition perform its side effects atomically (auto-generating
      submissions, queuing notifications, etc.).
    • Opening a campaign is the heavy operation: it expands the
      audience query into N ReportSubmission rows AND inserts the
      reminder/final notifications into ScheduledNotification, all in
      one transaction. Failure mid-way → nothing lands.
"""
import json
from datetime import timedelta

from django.db import connection, transaction
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .serializers import ReportCampaignSerializer
from accounts.views import audit


# ----------------------------------------------------------------------
# JSONB coercion
# ----------------------------------------------------------------------
# psycopg2's JSONB → Python conversion isn't always wired up the way
# Django's ORM expects when you go through `connection.cursor()`. In
# practice we sometimes get the raw JSON text back as a Python str
# instead of a parsed dict — which then explodes the first time we try
# `**scope_filter` for kwargs unpacking. This helper makes every read
# site idempotent: pass anything (None / dict / JSON string) and you
# always get a dict back.
# ----------------------------------------------------------------------
def _coerce_jsonb(value):
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


# ----------------------------------------------------------------------
# Permission helpers
# ----------------------------------------------------------------------
def _can_manage(user):
    return user.is_authenticated and user.has_litrix_perm('manage_campaigns')


def _can_view(user):
    return user.is_authenticated and (
        user.has_litrix_perm('view_campaign_reports')
        or user.has_litrix_perm('manage_campaigns')
    )


# ----------------------------------------------------------------------
# Audience expansion — translate ScopeFilter into a list of UserIDs
# ----------------------------------------------------------------------
def _expand_audience(cur, tenant_id, scope_type, scope_filter):
    """
    Return the set of UserIDs that match the campaign's scope.

    Always restricts to Researchers in the tenant — Deans/HoDs/Admins
    don't get verification submissions even if explicitly listed (they
    don't have a Scholar/ORCID-driven paper list to verify).
    """
    scope_filter = _coerce_jsonb(scope_filter)

    if scope_type == 'all':
        cur.execute(
            '''SELECT "UserID" FROM "Users"
               WHERE "UserType" = 'Researcher'
                 AND "IsActive" = TRUE
                 AND "TenantID" = %s''',
            [tenant_id],
        )
    elif scope_type == 'department':
        # scope_filter.department_ids: list[int]
        dept_ids = scope_filter.get('department_ids') or []
        if not dept_ids:
            return []
        cur.execute(
            '''SELECT DISTINCT u."UserID" FROM "Users" u
               JOIN "Works_In" w ON w."UserID" = u."UserID"
                                AND w."IsCurrentPosition" = TRUE
               WHERE u."UserType" = 'Researcher'
                 AND u."IsActive" = TRUE
                 AND u."TenantID" = %s
                 AND w."DepartmentID" = ANY(%s)''',
            [tenant_id, dept_ids],
        )
    elif scope_type == 'custom':
        user_ids = scope_filter.get('user_ids') or []
        if not user_ids:
            return []
        cur.execute(
            '''SELECT "UserID" FROM "Users"
               WHERE "UserType" = 'Researcher'
                 AND "IsActive" = TRUE
                 AND "TenantID" = %s
                 AND "UserID" = ANY(%s)''',
            [tenant_id, user_ids],
        )
    else:
        return []

    return [r[0] for r in cur.fetchall()]


# ----------------------------------------------------------------------
# List + Create
# ----------------------------------------------------------------------
@api_view(['GET', 'POST'])
def campaign_list_create(request):
    if request.method == 'GET':
        if not _can_view(request.user):
            return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
        return _list_campaigns(request)

    # POST
    if not _can_manage(request.user):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
    return _create_campaign(request)


def _list_campaigns(request):
    """
    List campaigns visible to the caller. Includes inline submission
    stats so the frontend can render progress bars without a second
    round-trip per campaign.
    """
    status_filter = (request.query_params.get('status') or '').strip()
    where = ['c."TenantID" = %s']
    params = [request.user.tenant_id]
    if status_filter:
        where.append('c."Status" = %s')
        params.append(status_filter)

    with connection.cursor() as cur:
        cur.execute(f'''
            SELECT
                c."CampaignID", c."Title", c."Description",
                c."TargetYears",
                c."OpensAt", c."ClosesAt",
                c."Status", c."ScopeType", c."ScopeFilter",
                c."CreatedByUserID", c."CreatedAt",
                c."ClosedAt", c."ArchivedAt",
                COALESCE(s.total, 0)     AS submissions_total,
                COALESCE(s.submitted, 0) AS submissions_submitted,
                COALESCE(s.late, 0)      AS submissions_late
            FROM "ReportCampaign" c
            LEFT JOIN (
                SELECT
                    "CampaignID",
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE "Status" = 'submitted') AS submitted,
                    COUNT(*) FILTER (WHERE "IsLate" = TRUE) AS late
                FROM "ReportSubmission"
                GROUP BY "CampaignID"
            ) s ON s."CampaignID" = c."CampaignID"
            WHERE {' AND '.join(where)}
            ORDER BY c."CreatedAt" DESC
        ''', params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    # Normalize column-case for the serializer (which uses snake_case).
    # We do the mapping inline to avoid a round-trip through the ORM.
    out = []
    for r in rows:
        out.append({
            'campaign_id':           r['CampaignID'],
            'title':                 r['Title'],
            'description':           r['Description'],
            'target_years':          r['TargetYears'],
            'opens_at':              r['OpensAt'],
            'closes_at':             r['ClosesAt'],
            'status':                r['Status'],
            'scope_type':            r['ScopeType'],
            # Normalize JSONB so the frontend always receives a dict
            # (or null), never a raw JSON string.
            'scope_filter':          _coerce_jsonb(r['ScopeFilter']),
            'created_by_user_id':    r['CreatedByUserID'],
            'created_at':            r['CreatedAt'],
            'closed_at':             r['ClosedAt'],
            'archived_at':           r['ArchivedAt'],
            'submissions_total':     r['submissions_total'],
            'submissions_submitted': r['submissions_submitted'],
            'submissions_late':      r['submissions_late'],
        })

    return Response({'campaigns': out})


def _create_campaign(request):
    s = ReportCampaignSerializer(data=request.data)
    s.is_valid(raise_exception=True)
    d = s.validated_data

    with connection.cursor() as cur:
        cur.execute('''
            INSERT INTO "ReportCampaign"
                ("TenantID", "Title", "Description", "TargetYears",
                 "OpensAt", "ClosesAt", "Status",
                 "ScopeType", "ScopeFilter",
                 "CreatedByUserID")
            VALUES (%s, %s, %s, %s,
                    %s, %s, 'draft',
                    %s, %s::jsonb,
                    %s)
            RETURNING "CampaignID"
        ''', [
            request.user.tenant_id,
            d['title'],
            d.get('description'),
            d['target_years'],
            d['opens_at'],
            d['closes_at'],
            d.get('scope_type', 'all'),
            json.dumps(d.get('scope_filter') or {}),
            request.user.user_id,
        ])
        campaign_id = cur.fetchone()[0]

    audit(
        request.user.user_id, request.user.tenant_id,
        'campaign.create', 'ReportCampaign', campaign_id,
        {'title': d['title'], 'target_years': d['target_years']},
        request=request,
    )

    return Response({'campaign_id': campaign_id, 'status': 'draft'},
                    status=status.HTTP_201_CREATED)


# ----------------------------------------------------------------------
# Detail + Edit
# ----------------------------------------------------------------------
@api_view(['GET', 'PATCH'])
def campaign_detail(request, campaign_id):
    if request.method == 'GET':
        if not _can_view(request.user):
            return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
        return _detail(request, campaign_id)

    if not _can_manage(request.user):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
    return _edit(request, campaign_id)


def _detail(request, campaign_id):
    with connection.cursor() as cur:
        cur.execute('''
            SELECT
                c."CampaignID", c."Title", c."Description",
                c."TargetYears", c."OpensAt", c."ClosesAt",
                c."Status", c."ScopeType", c."ScopeFilter",
                c."CreatedByUserID", c."CreatedAt",
                c."ClosedAt", c."ArchivedAt",
                COALESCE(s.total, 0), COALESCE(s.submitted, 0), COALESCE(s.late, 0)
            FROM "ReportCampaign" c
            LEFT JOIN (
                SELECT "CampaignID",
                       COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE "Status" = 'submitted') AS submitted,
                       COUNT(*) FILTER (WHERE "IsLate" = TRUE) AS late
                FROM "ReportSubmission"
                GROUP BY "CampaignID"
            ) s ON s."CampaignID" = c."CampaignID"
            WHERE c."CampaignID" = %s AND c."TenantID" = %s
        ''', [campaign_id, request.user.tenant_id])
        row = cur.fetchone()
        if not row:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

    return Response({
        'campaign_id':           row[0],
        'title':                 row[1],
        'description':           row[2],
        'target_years':          row[3],
        'opens_at':              row[4],
        'closes_at':             row[5],
        'status':                row[6],
        'scope_type':            row[7],
        'scope_filter':          _coerce_jsonb(row[8]),
        'created_by_user_id':    row[9],
        'created_at':            row[10],
        'closed_at':             row[11],
        'archived_at':           row[12],
        'submissions_total':     row[13],
        'submissions_submitted': row[14],
        'submissions_late':      row[15],
    })


def _edit(request, campaign_id):
    """
    Edit a draft campaign. Once a campaign is `active`, edits are
    refused — submissions and notifications have already gone out, and
    silently changing the target_years would invalidate everything.
    """
    with connection.cursor() as cur:
        cur.execute(
            'SELECT "Status" FROM "ReportCampaign" '
            'WHERE "CampaignID" = %s AND "TenantID" = %s',
            [campaign_id, request.user.tenant_id],
        )
        row = cur.fetchone()
        if not row:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        if row[0] != 'draft':
            return Response(
                {'error': f'Cannot edit a campaign in status={row[0]}. '
                          'Only drafts are editable.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

    # Build a partial-update from whatever the client sent
    allowed = {'title', 'description', 'target_years', 'opens_at',
               'closes_at', 'scope_type', 'scope_filter'}
    payload = {k: v for k, v in request.data.items() if k in allowed}
    if not payload:
        return Response({'error': 'Nothing to update'}, status=400)

    # Re-run validation through the serializer
    s = ReportCampaignSerializer(data=payload, partial=True)
    s.is_valid(raise_exception=True)
    d = s.validated_data

    sets = []
    params = []
    column_map = {
        'title':         '"Title"',
        'description':   '"Description"',
        'target_years':  '"TargetYears"',
        'opens_at':      '"OpensAt"',
        'closes_at':     '"ClosesAt"',
        'scope_type':    '"ScopeType"',
    }
    for k, col in column_map.items():
        if k in d:
            sets.append(f'{col} = %s')
            params.append(d[k])
    if 'scope_filter' in d:
        sets.append('"ScopeFilter" = %s::jsonb')
        params.append(json.dumps(d['scope_filter'] or {}))

    if not sets:
        return Response({'error': 'Nothing to update'}, status=400)

    params.extend([campaign_id, request.user.tenant_id])
    with connection.cursor() as cur:
        cur.execute(
            f'UPDATE "ReportCampaign" SET {", ".join(sets)} '
            f'WHERE "CampaignID" = %s AND "TenantID" = %s',
            params,
        )

    audit(
        request.user.user_id, request.user.tenant_id,
        'campaign.edit', 'ReportCampaign', campaign_id,
        list(d.keys()), request=request,
    )
    return Response({'message': 'Updated'})


# ----------------------------------------------------------------------
# Open — the big transition
# ----------------------------------------------------------------------
@api_view(['POST'])
def campaign_open(request, campaign_id):
    """
    Transition a draft campaign to active.

    Side effects (all in one transaction):
        1. Status: draft → active. OpensAt clamped to NOW() if past.
        2. Auto-generate one ReportSubmission per matched researcher.
        3. Queue three notifications:
             - 'campaign_open'     → send NOW
             - 'campaign_reminder' → send 3 days before ClosesAt (if room)
             - 'campaign_final'    → send 24h before ClosesAt (if room)
    """
    if not _can_manage(request.user):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)

    now = timezone.now()

    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(
                '''SELECT "CampaignID", "Title", "Status",
                          "OpensAt", "ClosesAt", "ScopeType", "ScopeFilter"
                   FROM "ReportCampaign"
                   WHERE "CampaignID" = %s AND "TenantID" = %s
                   FOR UPDATE''',
                [campaign_id, request.user.tenant_id],
            )
            row = cur.fetchone()
            if not row:
                return Response({'error': 'Not found'},
                                status=status.HTTP_404_NOT_FOUND)
            cid, title, current_status, opens_at, closes_at, scope_type, scope_filter = row
            # Normalize JSONB at the read boundary — see _coerce_jsonb docstring.
            scope_filter = _coerce_jsonb(scope_filter)

            if current_status != 'draft':
                return Response(
                    {'error': f'Cannot open a campaign in status={current_status}. '
                              'Only drafts can be opened.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if closes_at <= now:
                return Response(
                    {'error': 'ClosesAt has already passed. Update it first.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # 1) Activate the campaign. Clamp OpensAt to NOW() if it
            #    was scheduled in the past (admin clicked Open Now).
            effective_opens = opens_at if opens_at and opens_at > now else now
            cur.execute(
                '''UPDATE "ReportCampaign"
                   SET "Status" = 'active', "OpensAt" = %s
                   WHERE "CampaignID" = %s''',
                [effective_opens, campaign_id],
            )

            # 2) Expand audience → bulk-insert submissions
            user_ids = _expand_audience(
                cur, request.user.tenant_id, scope_type, scope_filter,
            )
            if user_ids:
                # ON CONFLICT lets us re-run the open operation safely
                # if the admin reopens an archived campaign later.
                cur.executemany(
                    '''INSERT INTO "ReportSubmission" ("CampaignID", "UserID")
                       VALUES (%s, %s)
                       ON CONFLICT ("CampaignID", "UserID") DO NOTHING''',
                    [(campaign_id, uid) for uid in user_ids],
                )

            # 3) Schedule the three notifications. The worker uses
            #    RelatedCampaignID to resolve recipients — so the
            #    audience JSON here is mostly informational. We still
            #    fold scope_type in so the audit trail is clear.
            audience_with_type = {
                **scope_filter,
                'campaign_id': campaign_id,
                'scope_type':  scope_type,
            }
            target_json = json.dumps(audience_with_type)

            campaign_title = title  # already a str
            schedule_rows = [
                # Open immediately
                ('campaign_open',
                 f'تم فتح نافذة التقارير: {campaign_title}',
                 f'تم فتح نافذة التقارير لـ "{campaign_title}". '
                 f'الرجاء مراجعة قائمة أبحاثك وتسليم تقريرك قبل تاريخ الإغلاق.',
                 now),
                # 3-day reminder (only if there's room)
                ('campaign_reminder',
                 f'تذكير: تقريرك ينتهي خلال 3 أيام — {campaign_title}',
                 f'لم تقم بعد بتسليم تقريرك لـ "{campaign_title}". '
                 f'النافذة تغلق بعد 3 أيام.',
                 closes_at - timedelta(days=3)),
                # Final 24h warning
                ('campaign_final',
                 f'تذكير أخير: التقرير ينتهي خلال 24 ساعة — {campaign_title}',
                 f'الفرصة الأخيرة لتسليم تقريرك لـ "{campaign_title}".',
                 closes_at - timedelta(hours=24)),
            ]

            for ntype, ntitle, nbody, send_at in schedule_rows:
                # Skip reminders whose send time is already in the past
                # (campaign opened too close to deadline).
                if send_at <= now and ntype != 'campaign_open':
                    continue
                cur.execute(
                    '''INSERT INTO "ScheduledNotification"
                       ("TenantID", "Title", "Body", "NotificationType",
                        "TargetAudience", "SendAt",
                        "RelatedCampaignID", "CreatedByUserID")
                       VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s)''',
                    [
                        request.user.tenant_id, ntitle, nbody, ntype,
                        target_json, send_at,
                        campaign_id, request.user.user_id,
                    ],
                )

    audit(
        request.user.user_id, request.user.tenant_id,
        'campaign.open', 'ReportCampaign', campaign_id,
        {'submissions_created': len(user_ids)},
        request=request,
    )
    return Response({
        'message':             'Campaign opened',
        'submissions_created': len(user_ids),
        'status':              'active',
    })


# ----------------------------------------------------------------------
# Close
# ----------------------------------------------------------------------
@api_view(['POST'])
def campaign_close(request, campaign_id):
    """
    Active → closed. Optional payload { close_at: ISO timestamp } lets
    admin "close now" (default) or explicitly set the timestamp.

    No automatic submission lock — submissions get their IsLate flag
    at submit time. Closing a campaign just prevents new submissions
    (enforced in my_reports_views.submit_submission).
    """
    if not _can_manage(request.user):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)

    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(
                '''SELECT "Status" FROM "ReportCampaign"
                   WHERE "CampaignID" = %s AND "TenantID" = %s
                   FOR UPDATE''',
                [campaign_id, request.user.tenant_id],
            )
            row = cur.fetchone()
            if not row:
                return Response({'error': 'Not found'},
                                status=status.HTTP_404_NOT_FOUND)
            if row[0] != 'active':
                return Response(
                    {'error': f'Cannot close a campaign in status={row[0]}. '
                              'Only active campaigns can be closed.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            cur.execute(
                '''UPDATE "ReportCampaign"
                   SET "Status" = 'closed',
                       "ClosedAt" = NOW(),
                       "ClosesAt" = NOW()
                   WHERE "CampaignID" = %s''',
                [campaign_id],
            )

    audit(
        request.user.user_id, request.user.tenant_id,
        'campaign.close', 'ReportCampaign', campaign_id,
        request=request,
    )
    return Response({'message': 'Campaign closed', 'status': 'closed'})


# ----------------------------------------------------------------------
# Submissions list (admin view)
# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# Excel export — per-campaign workbook
# ----------------------------------------------------------------------
@api_view(['GET'])
def campaign_export(request, campaign_id):
    """
    GET /api/campaigns/<id>/export/

    Builds a 5-sheet xlsx workbook for download:
        Overview         — campaign metadata + aggregate counts
        Researchers      — one row per submission (all statuses)
        Confirmed Papers — papers researchers said "this is mine"
        Not Mine         — papers researchers rejected (cleanup queue)
        Missing Papers   — papers researchers added that we didn't have

    Why the cleanup/missing sheets?
        The whole point of running a verification campaign isn't just
        the green checkmarks — it's catching misattributions AND
        finding papers we missed. The "Not Mine" sheet feeds the
        admin's Authors-cleanup workflow; the "Missing Papers" sheet
        feeds the manual-ingest workflow.
    """
    if not _can_view(request.user):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    with connection.cursor() as cur:
        # Confirm campaign exists + load metadata for the cover sheet
        cur.execute(
            '''SELECT "CampaignID", "Title", "Description",
                      "TargetYears", "OpensAt", "ClosesAt", "Status",
                      "ScopeType", "CreatedAt"
               FROM "ReportCampaign"
               WHERE "CampaignID" = %s AND "TenantID" = %s''',
            [campaign_id, request.user.tenant_id],
        )
        camp = cur.fetchone()
        if not camp:
            return Response({'error': 'Not found'},
                            status=status.HTTP_404_NOT_FOUND)

    wb = Workbook()
    wb.remove(wb.active)

    header_font  = Font(bold=True, color='FFFFFF')
    header_fill  = PatternFill('solid', fgColor='1D1D1F')
    header_align = Alignment(horizontal='center', vertical='center', wrap_text=True)
    title_font   = Font(bold=True, size=16, color='1D1D1F')
    label_font   = Font(bold=True, color='86868B')

    def style_header(ws, ncols):
        for col in range(1, ncols + 1):
            c = ws.cell(row=1, column=col)
            c.font = header_font
            c.fill = header_fill
            c.alignment = header_align
        ws.row_dimensions[1].height = 24

    def set_widths(ws, widths):
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[chr(64 + i)].width = w

    # =================================================================
    # Sheet 1: Overview
    # =================================================================
    ws = wb.create_sheet('Overview', 0)
    ws['A1'] = camp[1]  # Title
    ws['A1'].font = title_font
    ws.row_dimensions[1].height = 28
    if camp[2]:
        ws['A2'] = camp[2]  # Description
        ws['A2'].font = label_font

    # Metadata block
    meta = [
        ('Target Years',  ', '.join(str(y) for y in camp[3] or [])),
        ('Opens At',      camp[4].strftime('%Y-%m-%d %H:%M') if camp[4] else '—'),
        ('Closes At',     camp[5].strftime('%Y-%m-%d %H:%M') if camp[5] else '—'),
        ('Status',        camp[6]),
        ('Scope',         camp[7]),
        ('Created',       camp[8].strftime('%Y-%m-%d') if camp[8] else '—'),
    ]
    for i, (k, v) in enumerate(meta, start=4):
        ws.cell(row=i, column=1, value=k).font = label_font
        ws.cell(row=i, column=2, value=v)
    ws.column_dimensions['A'].width = 18
    ws.column_dimensions['B'].width = 40

    # Aggregate stats
    with connection.cursor() as cur:
        cur.execute(
            '''SELECT
                 COUNT(*)                                           AS total,
                 COUNT(*) FILTER (WHERE s."Status" = 'submitted')   AS submitted,
                 COUNT(*) FILTER (WHERE s."IsLate" = TRUE)          AS late,
                 (SELECT COUNT(*) FROM "ReportPaperDecision" rd
                   JOIN "ReportSubmission" rs ON rs."SubmissionID" = rd."SubmissionID"
                   WHERE rs."CampaignID" = %s AND rd."Decision" = 'confirmed')  AS confirmed,
                 (SELECT COUNT(*) FROM "ReportPaperDecision" rd
                   JOIN "ReportSubmission" rs ON rs."SubmissionID" = rd."SubmissionID"
                   WHERE rs."CampaignID" = %s AND rd."Decision" = 'not_mine')   AS not_mine,
                 (SELECT COUNT(*) FROM "ReportPaperDecision" rd
                   JOIN "ReportSubmission" rs ON rs."SubmissionID" = rd."SubmissionID"
                   WHERE rs."CampaignID" = %s AND rd."Decision" = 'missing')    AS missing
               FROM "ReportSubmission" s
               WHERE s."CampaignID" = %s''',
            [campaign_id] * 4,
        )
        stats = cur.fetchone()

    stats_block = [
        ('Researchers in scope', stats[0]),
        ('Submitted',            stats[1]),
        ('Late submissions',     stats[2]),
        ('Confirmed papers',     stats[3]),
        ('Not-mine decisions',   stats[4]),
        ('Missing-paper reports', stats[5]),
    ]
    for i, (k, v) in enumerate(stats_block, start=12):
        ws.cell(row=i, column=1, value=k).font = label_font
        ws.cell(row=i, column=2, value=v)

    # =================================================================
    # Sheet 2: Researchers — submission status per person
    # =================================================================
    ws = wb.create_sheet('Researchers')
    ws.append([
        'Name (AR)', 'Name (EN)', 'Email', 'Litrix ID',
        'Department', 'Status', 'Submitted At', 'Late',
        'Confirmed', 'Not Mine', 'Missing',
    ])
    style_header(ws, 11)
    with connection.cursor() as cur:
        cur.execute(
            '''SELECT
                 u."FullName_Ar",
                 TRIM(CONCAT_WS(' ', u."FirstName", u."LastName")) AS full_name_en,
                 u."Email", u."Litrix_ID",
                 d."DepartmentName",
                 s."Status",
                 s."SubmittedAt",
                 s."IsLate",
                 (SELECT COUNT(*) FROM "ReportPaperDecision" rd
                   WHERE rd."SubmissionID" = s."SubmissionID"
                     AND rd."Decision" = 'confirmed') AS c_count,
                 (SELECT COUNT(*) FROM "ReportPaperDecision" rd
                   WHERE rd."SubmissionID" = s."SubmissionID"
                     AND rd."Decision" = 'not_mine')  AS n_count,
                 (SELECT COUNT(*) FROM "ReportPaperDecision" rd
                   WHERE rd."SubmissionID" = s."SubmissionID"
                     AND rd."Decision" = 'missing')   AS m_count
               FROM "ReportSubmission" s
               JOIN "Users" u ON u."UserID" = s."UserID"
               LEFT JOIN "Works_In" w ON w."UserID" = u."UserID"
                                     AND w."IsCurrentPosition" = TRUE
               LEFT JOIN "Department" d ON d."DepartmentID" = w."DepartmentID"
               WHERE s."CampaignID" = %s
               ORDER BY
                 CASE s."Status"
                   WHEN 'submitted' THEN 0
                   WHEN 'in_progress' THEN 1
                   WHEN 'reopened' THEN 2
                   ELSE 3
                 END,
                 u."FullName_Ar"''',
            [campaign_id],
        )
        for r in cur.fetchall():
            ws.append([
                r[0], r[1], r[2], r[3], r[4], r[5],
                r[6].strftime('%Y-%m-%d %H:%M') if r[6] else '',
                'Yes' if r[7] else '',
                r[8], r[9], r[10],
            ])
    set_widths(ws, [25, 25, 30, 12, 22, 14, 18, 6, 10, 10, 10])

    # =================================================================
    # Sheet 3: Confirmed Papers
    # =================================================================
    ws = wb.create_sheet('Confirmed Papers')
    ws.append([
        'Researcher (AR)', 'Department', 'Paper Title', 'Year',
        'Journal', 'Quartile', 'Indexing', 'Citations', 'DOI',
    ])
    style_header(ws, 9)
    with connection.cursor() as cur:
        cur.execute(
            '''SELECT
                 u."FullName_Ar",
                 d."DepartmentName",
                 rp."Title",
                 rp."PubYear",
                 COALESCE(j."JournalName", rp."RawData_Log"->>'publication') AS journal,
                 jr."Quartile",
                 rp."Indexing",
                 COALESCE(
                     ("RawData_Log"->'cited_by'->>'value')::int,
                     ("RawData_Log"->>'cited_by_count')::int,
                     0
                 ) AS citations,
                 rp."DOI"
               FROM "ReportPaperDecision" rd
               JOIN "ReportSubmission" rs ON rs."SubmissionID" = rd."SubmissionID"
               JOIN "Users" u             ON u."UserID"        = rs."UserID"
               JOIN "ResearchPaper" rp    ON rp."PaperID"      = rd."PaperID"
               LEFT JOIN "Works_In" w ON w."UserID" = u."UserID"
                                     AND w."IsCurrentPosition" = TRUE
               LEFT JOIN "Department"      d ON d."DepartmentID" = w."DepartmentID"
               LEFT JOIN "Journals"        j ON j."JournalID"    = rp."JournalID"
               LEFT JOIN "JournalRankings" jr ON jr."JournalID"  = rp."JournalID"
               WHERE rs."CampaignID" = %s AND rd."Decision" = 'confirmed'
               ORDER BY u."FullName_Ar", rp."PubYear" DESC NULLS LAST''',
            [campaign_id],
        )
        for r in cur.fetchall():
            ws.append(list(r))
    set_widths(ws, [25, 22, 60, 8, 28, 10, 10, 10, 30])

    # =================================================================
    # Sheet 4: Not Mine — admin cleanup queue
    # =================================================================
    ws = wb.create_sheet('Not Mine')
    ws.append([
        'Researcher (AR)', 'Department', 'Paper Title',
        'Year', 'Journal', 'DOI', 'Researcher Note',
    ])
    style_header(ws, 7)
    with connection.cursor() as cur:
        cur.execute(
            '''SELECT
                 u."FullName_Ar",
                 d."DepartmentName",
                 rp."Title",
                 rp."PubYear",
                 COALESCE(j."JournalName", rp."RawData_Log"->>'publication'),
                 rp."DOI",
                 rd."Note"
               FROM "ReportPaperDecision" rd
               JOIN "ReportSubmission" rs ON rs."SubmissionID" = rd."SubmissionID"
               JOIN "Users" u             ON u."UserID"        = rs."UserID"
               JOIN "ResearchPaper" rp    ON rp."PaperID"      = rd."PaperID"
               LEFT JOIN "Works_In" w ON w."UserID" = u."UserID"
                                     AND w."IsCurrentPosition" = TRUE
               LEFT JOIN "Department" d ON d."DepartmentID" = w."DepartmentID"
               LEFT JOIN "Journals"   j ON j."JournalID"    = rp."JournalID"
               WHERE rs."CampaignID" = %s AND rd."Decision" = 'not_mine'
               ORDER BY u."FullName_Ar", rp."PubYear" DESC NULLS LAST''',
            [campaign_id],
        )
        for r in cur.fetchall():
            ws.append(list(r))
    set_widths(ws, [25, 22, 60, 8, 28, 30, 40])

    # =================================================================
    # Sheet 5: Missing Papers — researcher-reported additions
    # =================================================================
    ws = wb.create_sheet('Missing Papers')
    ws.append([
        'Researcher (AR)', 'Department', 'Title',
        'Year', 'DOI', 'Researcher Note', 'Resolved?',
    ])
    style_header(ws, 7)
    with connection.cursor() as cur:
        cur.execute(
            '''SELECT
                 u."FullName_Ar",
                 d."DepartmentName",
                 rd."MissingTitle",
                 rd."MissingYear",
                 rd."MissingDOI",
                 rd."Note",
                 CASE WHEN rd."MissingResolvedAt" IS NULL THEN 'Pending'
                      ELSE 'Resolved' END
               FROM "ReportPaperDecision" rd
               JOIN "ReportSubmission" rs ON rs."SubmissionID" = rd."SubmissionID"
               JOIN "Users" u             ON u."UserID"        = rs."UserID"
               LEFT JOIN "Works_In" w ON w."UserID" = u."UserID"
                                     AND w."IsCurrentPosition" = TRUE
               LEFT JOIN "Department" d ON d."DepartmentID" = w."DepartmentID"
               WHERE rs."CampaignID" = %s AND rd."Decision" = 'missing'
               ORDER BY u."FullName_Ar", rd."MissingYear" DESC NULLS LAST''',
            [campaign_id],
        )
        for r in cur.fetchall():
            ws.append(list(r))
    set_widths(ws, [25, 22, 60, 8, 30, 40, 12])

    # =================================================================
    # Stream the workbook back
    # =================================================================
    safe_title = ''.join(
        c if (c.isalnum() or c in ' -_') else '_'
        for c in (camp[1] or 'campaign')
    ).strip().replace(' ', '_')[:60]
    filename = f'campaign_{campaign_id}_{safe_title}.xlsx'

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    wb.save(response)

    audit(
        request.user.user_id, request.user.tenant_id,
        'campaign.export', 'ReportCampaign', campaign_id,
        request=request,
    )
    return response


@api_view(['GET'])
def campaign_submissions(request, campaign_id):
    """
    GET /api/campaigns/<id>/submissions/
    Returns one row per (campaign × researcher) for admin oversight.
    """
    if not _can_view(request.user):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)

    with connection.cursor() as cur:
        # Confirm campaign exists + tenant matches
        cur.execute(
            'SELECT 1 FROM "ReportCampaign" '
            'WHERE "CampaignID" = %s AND "TenantID" = %s',
            [campaign_id, request.user.tenant_id],
        )
        if not cur.fetchone():
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

        # Join with Users + Department for a list-ready payload
        cur.execute('''
            SELECT
                s."SubmissionID", s."UserID", s."Status",
                s."StartedAt", s."SubmittedAt", s."IsLate",
                u."FullName_Ar", u."Email", u."Litrix_ID",
                d."DepartmentName",
                (SELECT COUNT(*) FROM "ReportPaperDecision" rd
                  WHERE rd."SubmissionID" = s."SubmissionID"
                    AND rd."Decision" = 'confirmed')   AS confirmed_count,
                (SELECT COUNT(*) FROM "ReportPaperDecision" rd
                  WHERE rd."SubmissionID" = s."SubmissionID"
                    AND rd."Decision" = 'not_mine')    AS not_mine_count,
                (SELECT COUNT(*) FROM "ReportPaperDecision" rd
                  WHERE rd."SubmissionID" = s."SubmissionID"
                    AND rd."Decision" = 'missing')     AS missing_count
            FROM "ReportSubmission" s
            JOIN "Users" u ON u."UserID" = s."UserID"
            LEFT JOIN "Works_In" w ON w."UserID" = u."UserID"
                                  AND w."IsCurrentPosition" = TRUE
            LEFT JOIN "Department" d ON d."DepartmentID" = w."DepartmentID"
            WHERE s."CampaignID" = %s
            ORDER BY
                CASE s."Status"
                    WHEN 'pending'     THEN 0
                    WHEN 'in_progress' THEN 1
                    WHEN 'reopened'    THEN 2
                    WHEN 'submitted'   THEN 3
                END,
                u."FullName_Ar"
        ''', [campaign_id])
        cols = [d[0] for d in cur.description]
        submissions = [dict(zip(cols, r)) for r in cur.fetchall()]

    return Response({'submissions': submissions})
