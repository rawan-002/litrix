"""Notification list + read-state endpoints."""
from django.db import connection
from rest_framework.decorators import api_view
from rest_framework.response import Response


@api_view(['GET'])
def list_notifications(request):
    only_unread = request.GET.get('unread') == 'true'
    where = ['"UserID" = %s']
    params = [request.user.user_id]
    if only_unread:
        where.append('"IsRead" = FALSE')

    with connection.cursor() as cur:
        cur.execute(f'''
            SELECT "NotificationID", "Type", "Title", "Message",
                   "Metadata", "IsRead", "CreatedAt", "ReadAt"
            FROM "Notification"
            WHERE {" AND ".join(where)}
            ORDER BY "CreatedAt" DESC LIMIT 100
        ''', params)
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.execute(
            'SELECT COUNT(*) FROM "Notification" WHERE "UserID" = %s AND "IsRead" = FALSE',
            [request.user.user_id],
        )
        unread_count = cur.fetchone()[0]
    return Response({'notifications': rows, 'unread_count': unread_count})


@api_view(['POST'])
def mark_notification_read(request, notif_id):
    with connection.cursor() as cur:
        cur.execute('''
            UPDATE "Notification" SET "IsRead" = TRUE, "ReadAt" = NOW()
            WHERE "NotificationID" = %s AND "UserID" = %s AND "IsRead" = FALSE
        ''', [notif_id, request.user.user_id])
    return Response({'message': 'OK'})


@api_view(['POST'])
def mark_all_read(request):
    with connection.cursor() as cur:
        cur.execute('''
            UPDATE "Notification" SET "IsRead" = TRUE, "ReadAt" = NOW()
            WHERE "UserID" = %s AND "IsRead" = FALSE
        ''', [request.user.user_id])
    return Response({'message': 'All marked read'})
