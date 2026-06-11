"""
Researcher-facing endpoints for the Reporting Campaigns workflow.

Every query is scoped to request.user.user_id, so a researcher only ever
sees their own submissions — that SQL boundary is the access control here,
which is why IsAuthenticated alone is enough.

    GET    /api/my-reports/                         — my submissions
    GET    /api/my-reports/<sub_id>/                — detail + paper list
    POST   /api/my-reports/<sub_id>/decisions/      — record/update a decision
    POST   /api/my-reports/<sub_id>/missing/        — add a missing-paper entry
    DELETE /api/my-reports/<sub_id>/decisions/<id>/ — remove a decision (pre-submit)
    POST   /api/my-reports/<sub_id>/submit/         — final submit
"""
import json

from django.db import connection, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from accounts.views import audit


def _ownership_or_404(cur, submission_id, user_id):
    """
    Confirm the submission belongs to the caller. Returns the campaign info
    downstream queries need, or None if there's no match (404). The caller
    handles locking when it's needed.
    """
    cur.execute(
        '''SELECT s."SubmissionID", s."CampaignID", s."Status",
                  s."SubmittedAt",
                  c."Title", c."TargetYears", c."OpensAt", c."ClosesAt",
                  c."Status" AS campaign_status
           FROM "ReportSubmission" s
           JOIN "ReportCampaign"   c ON c."CampaignID" = s."CampaignID"
           WHERE s."SubmissionID" = %s AND s."UserID" = %s''',
        [submission_id, user_id],
    )
    return cur.fetchone()


def _is_editable(submission_status, campaign_status):
    """True while the researcher can still write decisions: submission is
    pending/in_progress/reopened and the campaign is still active."""
    return (
        submission_status in ('pending', 'in_progress', 'reopened')
        and campaign_status == 'active'
    )


@api_view(['GET'])
def my_submissions(request):
    """Active + recent submissions — feeds both the sidebar badge and the
    /my-reports landing page."""
    with connection.cursor() as cur:
        cur.execute('''
            SELECT
                s."SubmissionID", s."Status",
                s."StartedAt", s."SubmittedAt", s."IsLate",
                c."CampaignID", c."Title", c."Description",
                c."TargetYears", c."ClosesAt", c."Status" AS campaign_status,
                (SELECT COUNT(*) FROM "ReportPaperDecision" rd
                  WHERE rd."SubmissionID" = s."SubmissionID")
                  AS decisions_count
            FROM "ReportSubmission" s
            JOIN "ReportCampaign"   c ON c."CampaignID" = s."CampaignID"
            WHERE s."UserID" = %s
              AND c."Status" IN ('active', 'closed')
            ORDER BY
                CASE c."Status"
                    WHEN 'active' THEN 0
                    WHEN 'closed' THEN 1
                END,
                c."ClosesAt" DESC
        ''', [request.user.user_id])
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    # Count pending/in-progress for the sidebar badge.
    pending_count = sum(
        1 for r in rows
        if r['Status'] in ('pending', 'in_progress', 'reopened')
        and r['campaign_status'] == 'active'
    )

    return Response({
        'submissions':   rows,
        'pending_count': pending_count,
    })


@api_view(['GET'])
def submission_detail(request, submission_id):
    """
    Full submission payload: submission + campaign metadata, the
    auto-populated paper list (this user's papers within the campaign's
    target years), each paper's existing decision (LEFT JOIN, null if
    untouched), and the missing-paper entries this user added. The frontend
    renders each paper as a Confirm/Not-mine card with missing in its own
    section.
    """
    with connection.cursor() as cur:
        info = _ownership_or_404(cur, submission_id, request.user.user_id)
        if not info:
            return Response({'error': 'Not found'},
                            status=status.HTTP_404_NOT_FOUND)
        (_, campaign_id, sub_status, submitted_at,
         c_title, target_years, c_opens, c_closes, c_status) = info

        # Auto-populated papers, with any existing decision joined in.
        cur.execute('''
            SELECT
                rp."PaperID", rp."Title", rp."Title_En", rp."DOI",
                rp."PubYear", rp."Source", rp."Indexing",
                COALESCE(j."JournalName", rp."RawData_Log"->>'publication')
                    AS journal_name,
                jr."Quartile",
                COALESCE(
                    ("RawData_Log"->'cited_by'->>'value')::int,
                    ("RawData_Log"->>'cited_by_count')::int,
                    0
                ) AS citations,
                d."DecisionID", d."Decision", d."Note", d."DecidedAt"
            FROM "Authors" a
            JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
            LEFT JOIN "Journals" j ON j."JournalID" = rp."JournalID"
            LEFT JOIN "JournalRankings" jr ON jr."JournalID" = rp."JournalID"
            LEFT JOIN "ReportPaperDecision" d
                ON d."SubmissionID" = %s
               AND d."PaperID"      = rp."PaperID"
            WHERE a."UserID" = %s
              AND rp."PubYear" = ANY(%s)
            ORDER BY rp."PubYear" DESC NULLS LAST, rp."Title"
        ''', [submission_id, request.user.user_id, target_years])

        papers = []
        for r in cur.fetchall():
            papers.append({
                'paper_id':     r[0],
                'title':        r[1],
                'title_en':     r[2],
                'doi':          r[3],
                'pub_year':     r[4],
                'source':       r[5],
                'indexing':     r[6],
                'journal_name': r[7],
                'quartile':     r[8],
                'citations':    r[9],
                'decision': {
                    'decision_id': r[10],
                    'decision':    r[11],
                    'note':        r[12],
                    'decided_at':  r[13],
                } if r[10] else None,
            })

        # Missing-paper entries (decision='missing', no paper_id).
        cur.execute('''
            SELECT "DecisionID", "MissingTitle", "MissingDOI",
                   "MissingYear", "Note", "DecidedAt",
                   "MissingResolvedAt", "MissingResolvedToPaperID"
            FROM "ReportPaperDecision"
            WHERE "SubmissionID" = %s AND "Decision" = 'missing'
            ORDER BY "DecidedAt" DESC
        ''', [submission_id])
        missing = [
            {
                'decision_id':  r[0],
                'title':        r[1],
                'doi':          r[2],
                'year':         r[3],
                'note':         r[4],
                'decided_at':   r[5],
                'resolved_at':  r[6],
                'resolved_to_paper_id': r[7],
            }
            for r in cur.fetchall()
        ]

    return Response({
        'submission': {
            'submission_id': submission_id,
            'status':        sub_status,
            'submitted_at':  submitted_at,
            'is_editable':   _is_editable(sub_status, c_status),
        },
        'campaign': {
            'campaign_id':   campaign_id,
            'title':         c_title,
            'target_years':  target_years,
            'opens_at':      c_opens,
            'closes_at':     c_closes,
            'status':        c_status,
        },
        'papers':  papers,
        'missing': missing,
    })


@api_view(['POST'])
def record_decision(request, submission_id):
    """
    UPSERT a decision for an existing paper.
    Body: { paper_id: int, decision: 'confirmed' | 'not_mine', note?: str }

    Idempotent — re-sending the same decision is a no-op; a different one
    updates in place, so researchers can change their mind until they submit.
    """
    paper_id  = request.data.get('paper_id')
    decision  = (request.data.get('decision') or '').strip()
    note      = request.data.get('note')

    if not paper_id:
        return Response({'error': 'paper_id required'}, status=400)
    if decision not in ('confirmed', 'not_mine'):
        return Response(
            {'error': 'decision must be confirmed or not_mine '
                      '(use /missing/ endpoint for missing papers)'},
            status=400,
        )

    with transaction.atomic():
        with connection.cursor() as cur:
            info = _ownership_or_404(cur, submission_id, request.user.user_id)
            if not info:
                return Response({'error': 'Not found'},
                                status=status.HTTP_404_NOT_FOUND)
            _, _, sub_status, _, _, target_years, _, _, c_status = info

            if not _is_editable(sub_status, c_status):
                return Response(
                    {'error': f'Submission is not editable (status={sub_status}, '
                              f'campaign_status={c_status})'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # The paper must belong to this researcher and fall in the
            # campaign's year window — blocks decisions on arbitrary papers.
            cur.execute(
                '''SELECT 1 FROM "Authors" a
                   JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
                   WHERE a."UserID"  = %s
                     AND a."PaperID" = %s
                     AND rp."PubYear" = ANY(%s)''',
                [request.user.user_id, paper_id, target_years],
            )
            if not cur.fetchone():
                return Response(
                    {'error': 'Paper is not in your auto-populated list'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # UPSERT on the UNIQUE (SubmissionID, PaperID) constraint.
            cur.execute(
                '''INSERT INTO "ReportPaperDecision"
                       ("SubmissionID", "PaperID", "Decision", "Note")
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT ("SubmissionID", "PaperID") DO UPDATE
                       SET "Decision"  = EXCLUDED."Decision",
                           "Note"      = EXCLUDED."Note",
                           "DecidedAt" = NOW()
                   RETURNING "DecisionID"''',
                [submission_id, paper_id, decision, note],
            )
            decision_id = cur.fetchone()[0]

            # First touch moves the submission to in_progress.
            if sub_status == 'pending':
                cur.execute(
                    '''UPDATE "ReportSubmission"
                       SET "Status" = 'in_progress', "StartedAt" = NOW()
                       WHERE "SubmissionID" = %s''',
                    [submission_id],
                )

    return Response({
        'decision_id': decision_id,
        'decision':    decision,
    })


@api_view(['POST'])
def add_missing(request, submission_id):
    """
    POST /api/my-reports/<sub_id>/missing/
    Body: { title: str, year: int, doi?: str, note?: str }
    Creates a decision row with PaperID=NULL for the admin to resolve later.
    """
    title = (request.data.get('title') or '').strip()
    year  = request.data.get('year')
    doi   = (request.data.get('doi') or '').strip() or None
    note  = request.data.get('note')

    if not title:
        return Response({'error': 'title required'}, status=400)
    try:
        year_int = int(year)
    except (TypeError, ValueError):
        return Response({'error': 'year required (integer)'}, status=400)

    with transaction.atomic():
        with connection.cursor() as cur:
            info = _ownership_or_404(cur, submission_id, request.user.user_id)
            if not info:
                return Response({'error': 'Not found'},
                                status=status.HTTP_404_NOT_FOUND)
            _, _, sub_status, _, _, target_years, _, _, c_status = info

            if not _is_editable(sub_status, c_status):
                return Response(
                    {'error': 'Submission is not editable'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if year_int not in target_years:
                return Response(
                    {'error': f'year must be one of {target_years}'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            cur.execute(
                '''INSERT INTO "ReportPaperDecision"
                       ("SubmissionID", "PaperID", "Decision",
                        "MissingTitle", "MissingDOI", "MissingYear", "Note")
                   VALUES (%s, NULL, 'missing', %s, %s, %s, %s)
                   RETURNING "DecisionID"''',
                [submission_id, title, doi, year_int, note],
            )
            decision_id = cur.fetchone()[0]

            if sub_status == 'pending':
                cur.execute(
                    '''UPDATE "ReportSubmission"
                       SET "Status" = 'in_progress', "StartedAt" = NOW()
                       WHERE "SubmissionID" = %s''',
                    [submission_id],
                )

    return Response({'decision_id': decision_id, 'decision': 'missing'},
                    status=status.HTTP_201_CREATED)


@api_view(['DELETE'])
def delete_decision(request, submission_id, decision_id):
    """
    Remove a decision before submit — an accidental missing-paper entry, or
    undoing a confirmed/not_mine click.
    """
    with transaction.atomic():
        with connection.cursor() as cur:
            info = _ownership_or_404(cur, submission_id, request.user.user_id)
            if not info:
                return Response({'error': 'Not found'},
                                status=status.HTTP_404_NOT_FOUND)
            _, _, sub_status, _, _, _, _, _, c_status = info

            if not _is_editable(sub_status, c_status):
                return Response(
                    {'error': 'Submission is not editable'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            cur.execute(
                '''DELETE FROM "ReportPaperDecision"
                   WHERE "DecisionID" = %s AND "SubmissionID" = %s
                   RETURNING "DecisionID"''',
                [decision_id, submission_id],
            )
            if not cur.fetchone():
                return Response({'error': 'Decision not found'},
                                status=status.HTTP_404_NOT_FOUND)

    return Response({'message': 'Decision removed'})


@api_view(['POST'])
def submit_submission(request, submission_id):
    """
    Lock the submission: it goes read-only, further decisions are blocked
    (_is_editable returns False for 'submitted'), the admin gets a
    notification, and IsLate is set if we're past closes_at.

    Partial reports are allowed on purpose — we don't require a decision on
    every paper; researchers can come back and the admin can reopen.
    """
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(
                '''SELECT s."SubmissionID", s."Status", s."CampaignID",
                          c."Title", c."ClosesAt", c."Status",
                          c."CreatedByUserID", c."TenantID"
                   FROM "ReportSubmission" s
                   JOIN "ReportCampaign"   c ON c."CampaignID" = s."CampaignID"
                   WHERE s."SubmissionID" = %s AND s."UserID" = %s
                   FOR UPDATE OF s''',
                [submission_id, request.user.user_id],
            )
            row = cur.fetchone()
            if not row:
                return Response({'error': 'Not found'},
                                status=status.HTTP_404_NOT_FOUND)
            _, sub_status, campaign_id, c_title, c_closes_at, c_status, admin_id, tenant_id = row

            if sub_status == 'submitted':
                return Response(
                    {'error': 'Already submitted', 'submission_id': submission_id},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if c_status != 'active':
                return Response(
                    {'error': f'Campaign is {c_status}, cannot submit'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            now = timezone.now()
            is_late = c_closes_at and now > c_closes_at

            cur.execute(
                '''UPDATE "ReportSubmission"
                   SET "Status"      = 'submitted',
                       "SubmittedAt" = %s,
                       "IsLate"      = %s
                   WHERE "SubmissionID" = %s''',
                [now, is_late, submission_id],
            )

            # Notify the admin who opened the campaign. Include the
            # researcher's name so the inbox is useful without a click-through.
            # English copy, matching the "Research Reports" UI label.
            full_name = (request.user.full_name_ar
                         or request.user.get_full_name()
                         or request.user.email)
            cur.execute(
                '''INSERT INTO "Notification"
                       ("TenantID", "UserID", "Type", "Title",
                        "Message", "Metadata")
                   VALUES (%s, %s, 'campaign.submission_received',
                           %s, %s, %s::jsonb)''',
                [
                    tenant_id, admin_id,
                    f'Report received from {full_name}',
                    f'{full_name} submitted their report for "{c_title}".'
                    + (' (Late)' if is_late else ''),
                    json.dumps({
                        'campaign_id':   campaign_id,
                        'submission_id': submission_id,
                        'researcher_id': request.user.user_id,
                        'is_late':       bool(is_late),
                    }),
                ],
            )

    audit(
        request.user.user_id, request.user.tenant_id,
        'campaign.submit', 'ReportSubmission', submission_id,
        {'campaign_id': campaign_id, 'is_late': bool(is_late)},
        request=request,
    )
    return Response({
        'message':       'Submitted',
        'submission_id': submission_id,
        'submitted_at':  now,
        'is_late':       bool(is_late),
    })
