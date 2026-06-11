"""Role + permission management endpoints (RBAC admin)."""
from django.db import connection
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .common import audit


@api_view(['GET'])
def list_roles(request):
    if not request.user.has_litrix_perm('manage_users') and not request.user.has_litrix_perm('manage_roles'):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
    with connection.cursor() as cur:
        cur.execute('''
            SELECT "RoleID", "Name", "Description", "IsSystem"
            FROM "Role" WHERE "TenantID" = %s ORDER BY "RoleID"
        ''', [request.user.tenant_id])
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return Response({'roles': rows})


@api_view(['GET'])
def list_permissions(request):
    if not request.user.has_litrix_perm('manage_roles'):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
    with connection.cursor() as cur:
        cur.execute('SELECT "PermissionID", "Code", "Description", "Category" FROM "Permission" ORDER BY "Category", "Code"')
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return Response({'permissions': rows})


@api_view(['GET'])
def get_role_permissions(request, role_id):
    if not request.user.has_litrix_perm('manage_roles'):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
    with connection.cursor() as cur:
        cur.execute('''
            SELECT p."PermissionID" FROM "RolePermission" rp
            JOIN "Permission" p ON p."PermissionID" = rp."PermissionID"
            WHERE rp."RoleID" = %s
        ''', [role_id])
        ids = [r[0] for r in cur.fetchall()]
    return Response({'permission_ids': ids})


@api_view(['PUT'])
def set_role_permissions(request, role_id):
    if not request.user.has_litrix_perm('manage_roles'):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)

    permission_ids = request.data.get('permission_ids') or []
    with connection.cursor() as cur:
        cur.execute('''
            SELECT 1 FROM "Role" WHERE "RoleID" = %s AND "TenantID" = %s
        ''', [role_id, request.user.tenant_id])
        if not cur.fetchone():
            return Response({'error': 'Role not found'}, status=404)

        cur.execute('DELETE FROM "RolePermission" WHERE "RoleID" = %s', [role_id])
        for pid in permission_ids:
            cur.execute(
                'INSERT INTO "RolePermission" ("RoleID", "PermissionID") VALUES (%s, %s) ON CONFLICT DO NOTHING',
                [role_id, pid],
            )

    audit(request.user.user_id, request.user.tenant_id,
          'role.update_permissions', 'Role', role_id,
          {'count': len(permission_ids)}, request=request)
    return Response({'message': 'Updated', 'count': len(permission_ids)})


@api_view(['POST'])
def create_role(request):
    if not request.user.has_litrix_perm('manage_roles'):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)

    name = (request.data.get('name') or '').strip()
    description = (request.data.get('description') or '').strip()
    if not name:
        return Response({'error': 'Name required'}, status=400)

    try:
        with connection.cursor() as cur:
            cur.execute('''
                INSERT INTO "Role" ("TenantID", "Name", "Description", "IsSystem")
                VALUES (%s, %s, %s, FALSE) RETURNING "RoleID"
            ''', [request.user.tenant_id, name, description])
            role_id = cur.fetchone()[0]
    except psycopg2.IntegrityError:
        return Response({'error': 'Role name already exists'}, status=400)

    audit(request.user.user_id, request.user.tenant_id,
          'role.create', 'Role', role_id, {'name': name}, request=request)
    return Response({'role_id': role_id, 'name': name}, status=201)


@api_view(['DELETE'])
def delete_role(request, role_id):
    if not request.user.has_litrix_perm('manage_roles'):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
    with connection.cursor() as cur:
        cur.execute('SELECT "IsSystem" FROM "Role" WHERE "RoleID" = %s AND "TenantID" = %s',
                    [role_id, request.user.tenant_id])
        row = cur.fetchone()
        if not row:
            return Response({'error': 'Not found'}, status=404)
        if row[0]:
            return Response({'error': 'Cannot delete system role'}, status=400)
        cur.execute('UPDATE "Users" SET "RoleID" = NULL WHERE "RoleID" = %s', [role_id])
        cur.execute('DELETE FROM "Role" WHERE "RoleID" = %s', [role_id])
    audit(request.user.user_id, request.user.tenant_id,
          'role.delete', 'Role', role_id, request=request)
    return Response({'message': 'Deleted'})
