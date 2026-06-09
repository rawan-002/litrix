"""
Role-scoped invitation system.

Architecture:
    Admin → POST /api/auth/invitations/  → row inserted, token returned
    Invitee → GET  /api/auth/invitations/<token>/   → public lookup
    Invitee → POST /api/auth/register/    body { invite: token, ... }
            → register endpoint consumes the token, applies the
              pre-approved role, skips the manual approval queue.

The token is the approval. We trust it because:
    • only admins (or someone with `manage_users`) can create one
    • it's email-bound — register validates the form's email matches
    • single-use (UsedAt) + time-bound (ExpiresAt) + revocable
"""
import secrets
from datetime import datetime, timedelta, timezone

from django.db import connection
from rest_framework import status, permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response


# Tokens stay url-safe and high-entropy. 48 bytes → ~64 chars base64url.
TOKEN_LEN = 48

# Default lifetime for a new invitation. Long enough that admins can
# coordinate with the invitee, short enough to limit replay risk.
DEFAULT_TTL_DAYS = 14

# Roles the system allows admins to invite into. We hard-list them here
# so an admin can't accidentally create an "Admin" invitation just by
# mistyping a role name in the request body.
ALLOWED_ROLES = {'HoD', 'Dean', 'Researcher'}


def _now():
    return datetime.now(timezone.utc)


# ============================================================================
# Public endpoints
# ============================================================================
@api_view(['GET'])
@permission_classes([permissions.AllowAny])
def get_invitation(request, token: str):
    """
    Public token validator the registration page calls so it can pre-fill
    + lock the form. Never returns the inviter's identity beyond their
    name — no PII leakage from the token itself.
    """
    with connection.cursor() as cur:
        cur.execute('''
            SELECT i."InvitationID", i."InvitedEmail",
                   i."IntendedUserType",
                   r."Name"  AS role_name,
                   d."DepartmentID", d."DepartmentName",
                   i."ExpiresAt", i."UsedAt", i."RevokedAt"
            FROM "Invitation" i
            LEFT JOIN "Role"        r ON r."RoleID"       = i."IntendedRoleID"
            LEFT JOIN "Department"  d ON d."DepartmentID" = i."IntendedDepartmentID"
            WHERE i."Token" = %s
            LIMIT 1
        ''', [token])
        row = cur.fetchone()

    if not row:
        return Response({'valid': False, 'reason': 'not_found'}, status=404)

    (_, email, user_type, role_name,
     dept_id, dept_name, expires_at, used_at, revoked_at) = row

    if used_at:
        return Response({'valid': False, 'reason': 'already_used'})
    if revoked_at:
        return Response({'valid': False, 'reason': 'revoked'})
    if expires_at and expires_at < _now():
        return Response({'valid': False, 'reason': 'expired'})

    return Response({
        'valid':              True,
        'invited_email':      email,
        'intended_user_type': user_type,
        'intended_role':      role_name,
        'department_id':      dept_id,
        'department_name':    dept_name,
        'expires_at':         expires_at.isoformat() if expires_at else None,
    })


# ============================================================================
# Admin-only endpoints
# ============================================================================
@api_view(['GET', 'POST'])
def list_or_create_invitation(request):
    """
    GET  → list invitations (newest first), with usage state.
    POST → create new invitation. Body:
              { email, role, department_id?, ttl_days? }
              role ∈ {'HoD', 'Dean', 'Researcher'}
    """
    if not request.user.has_litrix_perm('manage_users'):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)

    if request.method == 'GET':
        return _list_invitations(request)
    return _create_invitation(request)


def _list_invitations(request):
    with connection.cursor() as cur:
        cur.execute('''
            SELECT i."InvitationID", i."Token", i."InvitedEmail",
                   i."IntendedUserType",
                   r."Name"  AS role_name,
                   d."DepartmentName",
                   i."ExpiresAt", i."UsedAt", i."RevokedAt", i."CreatedAt",
                   inv."FullName_Ar" AS invited_by_name,
                   used."Email"      AS used_by_email
            FROM "Invitation" i
            LEFT JOIN "Role"       r    ON r."RoleID"        = i."IntendedRoleID"
            LEFT JOIN "Department" d    ON d."DepartmentID"  = i."IntendedDepartmentID"
            LEFT JOIN "Users"      inv  ON inv."UserID"      = i."InvitedByUserID"
            LEFT JOIN "Users"      used ON used."UserID"     = i."UsedByUserID"
            WHERE i."TenantID" = %s
            ORDER BY i."CreatedAt" DESC
            LIMIT 100
        ''', [request.user.tenant_id])
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    # Annotate the human-readable status — easier than recomputing in JS.
    now = _now()
    for r in rows:
        if r['UsedAt']:
            r['status'] = 'used'
        elif r['RevokedAt']:
            r['status'] = 'revoked'
        elif r['ExpiresAt'] and r['ExpiresAt'] < now:
            r['status'] = 'expired'
        else:
            r['status'] = 'pending'

    return Response({'invitations': rows})


def _create_invitation(request):
    d         = request.data or {}
    email     = (d.get('email') or '').strip().lower()
    role      = (d.get('role')  or '').strip()
    dept_id   = d.get('department_id')
    ttl_days  = int(d.get('ttl_days') or DEFAULT_TTL_DAYS)

    if not email:
        return Response({'error': 'email is required'}, status=400)
    if role not in ALLOWED_ROLES:
        return Response(
            {'error': f'role must be one of {sorted(ALLOWED_ROLES)}'},
            status=400,
        )

    # Resolve the role to a RoleID for the current tenant.
    with connection.cursor() as cur:
        cur.execute(
            'SELECT "RoleID" FROM "Role" WHERE "Name" = %s AND "TenantID" = %s LIMIT 1',
            [role, request.user.tenant_id],
        )
        rrow = cur.fetchone()
        if not rrow:
            return Response(
                {'error': f"Role '{role}' not configured for this tenant"},
                status=400,
            )
        role_id = rrow[0]

        # Supersede any earlier still-pending invitation for this same
        # email in this tenant. Only the freshest link should stay live,
        # so an inbox can't accumulate several valid invitations (and the
        # consume step can't accidentally pick a stale one). Used or
        # already-revoked rows are left untouched.
        cur.execute('''
            UPDATE "Invitation"
               SET "RevokedAt" = NOW()
             WHERE "TenantID"           = %s
               AND LOWER("InvitedEmail") = %s
               AND "UsedAt"    IS NULL
               AND "RevokedAt" IS NULL
        ''', [request.user.tenant_id, email])

        # Generate a fresh url-safe token. Collision is astronomically
        # unlikely at 48 bytes but we still rely on the UNIQUE constraint
        # to hard-fail if the impossible happens.
        token      = secrets.token_urlsafe(TOKEN_LEN)
        expires_at = _now() + timedelta(days=ttl_days)

        cur.execute('''
            INSERT INTO "Invitation"
              ("TenantID", "Token", "InvitedEmail",
               "IntendedRoleID", "IntendedUserType",
               "IntendedDepartmentID", "InvitedByUserID", "ExpiresAt")
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING "InvitationID"
        ''', [
            request.user.tenant_id, token, email,
            role_id, role,
            dept_id or None,
            request.user.user_id,
            expires_at,
        ])
        invitation_id = cur.fetchone()[0]

    # Email the invitee with the registration link.
    from .email_service import send_invitation_email
    sent = send_invitation_email(
        to=email, role=role, token=token,
        inviter_name=request.user.get_full_name() or request.user.email,
    )

    return Response({
        'invitation_id': invitation_id,
        'token':         token,
        'expires_at':    expires_at.isoformat(),
        'email_sent':    sent,
    }, status=status.HTTP_201_CREATED)


@api_view(['DELETE'])
def revoke_invitation(request, invitation_id: int):
    if not request.user.has_litrix_perm('manage_users'):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)

    with connection.cursor() as cur:
        cur.execute('''
            UPDATE "Invitation"
               SET "RevokedAt" = NOW()
             WHERE "InvitationID" = %s
               AND "TenantID"     = %s
               AND "UsedAt"  IS NULL
               AND "RevokedAt" IS NULL
            RETURNING "InvitationID"
        ''', [invitation_id, request.user.tenant_id])
        if not cur.fetchone():
            return Response(
                {'error': 'Invitation not found or already used/revoked'},
                status=404,
            )

    return Response({'message': 'Revoked'})


# ============================================================================
# Token consumption — called from accounts.views.register
# ============================================================================
def consume_invitation(token: str, claimed_email: str):
    """
    Validate + lock + atomically mark used. Returns dict with the
    intended provisioning details, or None+reason on rejection.

    Caller (register endpoint) is responsible for actually creating
    the User row using the returned values, then calling
    `mark_invitation_used(invitation_id, new_user_id)` once the User
    has a UserID. We split it that way so the User insert can roll
    back without leaking a "used" flag.
    """
    if not token:
        return None, 'no_token'

    with connection.cursor() as cur:
        cur.execute('''
            SELECT "InvitationID", "InvitedEmail", "IntendedRoleID",
                   "IntendedUserType", "IntendedDepartmentID", "TenantID",
                   "ExpiresAt", "UsedAt", "RevokedAt"
            FROM "Invitation"
            WHERE "Token" = %s
            FOR UPDATE
        ''', [token])
        row = cur.fetchone()

    if not row:
        return None, 'not_found'

    (inv_id, invited_email, role_id, user_type, dept_id, tenant_id,
     expires_at, used_at, revoked_at) = row

    if used_at:
        return None, 'already_used'
    if revoked_at:
        return None, 'revoked'
    if expires_at and expires_at < _now():
        return None, 'expired'
    if (claimed_email or '').strip().lower() != (invited_email or '').lower():
        return None, 'email_mismatch'

    return {
        'invitation_id': inv_id,
        'tenant_id':     tenant_id,
        'role_id':       role_id,
        'user_type':     user_type,
        'department_id': dept_id,
        'invited_email': invited_email,
    }, None


def mark_invitation_used(invitation_id: int, new_user_id: int):
    with connection.cursor() as cur:
        cur.execute('''
            UPDATE "Invitation"
               SET "UsedAt"       = NOW(),
                   "UsedByUserID" = %s
             WHERE "InvitationID" = %s
        ''', [new_user_id, invitation_id])
