"""Shared helpers for the accounts views."""
import json

from django.db import connection


def audit(user_id, tenant_id, action, target_type=None, target_id=None, metadata=None, request=None):
    with connection.cursor() as cur:
        cur.execute('''
            INSERT INTO "AuditLog"
            ("TenantID", "UserID", "Action", "TargetType", "TargetID",
             "Metadata", "IpAddress", "UserAgent")
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
        ''', [
            tenant_id, user_id, action, target_type, target_id,
            json.dumps(metadata or {}),
            request.META.get('REMOTE_ADDR') if request else None,
            request.META.get('HTTP_USER_AGENT', '')[:500] if request else None,
        ])
