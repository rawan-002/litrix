"""
Admin author-reconciliation endpoints — the page where an admin works
through low-confidence co-author matches one at a time.

    GET  /api/admin/author-review-queue/             → list pending items
    POST /api/admin/author-review-queue/<id>/decide/ → CONFIRM / REJECT / SKIP

Permissions reuse manage_users (author linkage is a user-data integrity
operation; Admin and Dean already hold it). Each decision is one atomic
transaction — flip the queue row, on CONFIRM insert into Authors with
MappingCriteria='admin_reconcile', and write the AuditLog — so a failure
mid-way lands nothing. The list is paged (default 50/req).
"""
import json

from django.db import connection, transaction
from rest_framework import status as http
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.views import audit


# Permission helper (mirrors the campaign_views convention).
def _can_reconcile(user) -> bool:
    return user.is_authenticated and (
        user.has_litrix_perm('manage_users')
        or user.user_type == 'Admin'
    )


# GET /api/admin/author-review-queue/
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def review_queue_list(request):
    if not _can_reconcile(request.user):
        return Response({'error': 'permission_denied'}, status=http.HTTP_403_FORBIDDEN)

    status_filter = request.GET.get('status', 'PENDING').upper()
    if status_filter not in ('PENDING', 'CONFIRMED', 'REJECTED', 'SKIPPED', 'ALL'):
        return Response({'error': 'invalid_status'}, status=http.HTTP_400_BAD_REQUEST)

    try:
        limit = min(int(request.GET.get('limit', 50)), 200)
    except ValueError:
        limit = 50

    where = ''
    params = []
    if status_filter != 'ALL':
        where = 'WHERE q."Status" = %s'
        params.append(status_filter)

    with connection.cursor() as cur:
        cur.execute(f'''
            SELECT
                q."ReviewID",
                q."PaperID",
                rp."Title",
                q."ScrapedName",
                q."ScrapedAffiliation",
                q."SuggestedUserID",
                CONCAT(u."FirstName", ' ', u."LastName") AS suggested_name_en,
                u."FullName_Ar"                          AS suggested_name_ar,
                d."DepartmentName"                       AS suggested_department,
                q."SuggestedConfidence",
                q."SuggestedCriteria",
                q."Status",
                q."CreatedAt"
            FROM "AuthorReviewQueue" q
            LEFT JOIN "ResearchPaper" rp ON rp."PaperID"  = q."PaperID"
            LEFT JOIN "Users" u          ON u."UserID"    = q."SuggestedUserID"
            LEFT JOIN "Works_In" w       ON w."UserID"    = q."SuggestedUserID"
                                          AND w."IsCurrentPosition" = TRUE
            LEFT JOIN "Department" d     ON d."DepartmentID" = w."DepartmentID"
            {where}
            ORDER BY q."SuggestedConfidence" DESC, q."CreatedAt" DESC
            LIMIT %s
        ''', params + [limit])
        rows = cur.fetchall()

    items = [
        {
            'reviewId':             r[0],
            'paperId':              r[1],
            'paperTitle':           r[2] or '',
            'scrapedName':          r[3],
            'scrapedAffiliation':   r[4],
            'suggestedUserId':      r[5],
            'suggestedName':        (r[7] or r[6] or '').strip() or None,
            'suggestedDepartment':  r[8],
            'suggestedConfidence':  float(r[9]) if r[9] is not None else 0.0,
            'suggestedCriteria':    r[10],
            'status':               r[11],
            'createdAt':            r[12].isoformat() if r[12] else None,
        }
        for r in rows
    ]
    return Response(items)


# POST /api/admin/author-review-queue/<id>/decide/
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def review_queue_decide(request, review_id: int):
    if not _can_reconcile(request.user):
        return Response({'error': 'permission_denied'}, status=http.HTTP_403_FORBIDDEN)

    decision = (request.data.get('decision') or '').upper()
    if decision not in ('CONFIRMED', 'REJECTED', 'SKIPPED'):
        return Response({'error': 'invalid_decision'}, status=http.HTTP_400_BAD_REQUEST)

    notes = (request.data.get('notes') or '').strip() or None

    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute('''
                SELECT "ReviewID", "PaperID", "ScrapedName",
                       "SuggestedUserID", "SuggestedConfidence",
                       "Status"
                FROM "AuthorReviewQueue"
                WHERE "ReviewID" = %s
                FOR UPDATE
            ''', (review_id,))
            row = cur.fetchone()
            if not row:
                return Response({'error': 'not_found'}, status=http.HTTP_404_NOT_FOUND)

            (_rid, paper_id, scraped_name, suggested_user_id,
             suggested_confidence, current_status) = row

            if current_status != 'PENDING':
                return Response(
                    {'error': 'already_decided', 'currentStatus': current_status},
                    status=http.HTTP_409_CONFLICT,
                )

            # Update the queue row.
            cur.execute('''
                UPDATE "AuthorReviewQueue"
                SET "Status"           = %s,
                    "ReviewedByUserID" = %s,
                    "ReviewedAt"       = NOW(),
                    "ReviewerNotes"    = %s
                WHERE "ReviewID" = %s
            ''', (decision, request.user.user_id, notes, review_id))

            # On CONFIRMED, write the link into Authors — the admin decision
            # is the authoritative criteria and gets full confidence.
            if decision == 'CONFIRMED':
                if not suggested_user_id:
                    return Response(
                        {'error': 'no_suggested_user'},
                        status=http.HTTP_400_BAD_REQUEST,
                    )
                cur.execute('''
                    INSERT INTO "Authors" (
                        "UserID", "PaperID", "AuthorOrder",
                        "IsCorrespondingAuthor",
                        "MappingConfidence", "MappingCriteria",
                        "AuthorNameRaw", "Is_Verified"
                    )
                    VALUES (%s, %s, NULL, FALSE, 1.0, 'admin_reconcile', %s, TRUE)
                    ON CONFLICT ("UserID", "PaperID") DO UPDATE
                    SET "MappingConfidence" = GREATEST(
                            "Authors"."MappingConfidence", EXCLUDED."MappingConfidence"
                        ),
                        "MappingCriteria"   = 'admin_reconcile',
                        "Is_Verified"       = TRUE
                ''', (suggested_user_id, paper_id, scraped_name))

        # Audit outside the cursor block — audit() opens its own cursor.
        audit(
            user_id=request.user.user_id,
            tenant_id=getattr(request.user, 'tenant_id', 1),
            action=f'author_review.{decision.lower()}',
            target_type='AuthorReviewQueue',
            target_id=review_id,
            metadata={
                'paperId':             paper_id,
                'scrapedName':         scraped_name,
                'suggestedUserId':     suggested_user_id,
                'suggestedConfidence': float(suggested_confidence)
                                        if suggested_confidence is not None else None,
                'notes':               notes,
            },
            request=request,
        )

    return Response({'ok': True, 'reviewId': review_id, 'status': decision})


# GET /api/admin/author-review-queue/stats/
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def review_queue_stats(request):
    """Cheap counters for the header / nav badge."""
    if not _can_reconcile(request.user):
        return Response({'error': 'permission_denied'}, status=http.HTTP_403_FORBIDDEN)

    with connection.cursor() as cur:
        cur.execute('''
            SELECT "Status", COUNT(*)
            FROM "AuthorReviewQueue"
            GROUP BY "Status"
        ''')
        by_status = {r[0]: r[1] for r in cur.fetchall()}

        cur.execute('''
            SELECT COUNT(*) FROM "ResearchPaper" rp
            WHERE NOT EXISTS (
                SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID"
            )
        ''')
        orphans = cur.fetchone()[0]

    return Response({
        'pending':   by_status.get('PENDING', 0),
        'confirmed': by_status.get('CONFIRMED', 0),
        'rejected':  by_status.get('REJECTED', 0),
        'skipped':   by_status.get('SKIPPED', 0),
        'orphanedPapers': orphans,
    })
