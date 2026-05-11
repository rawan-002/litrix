from rest_framework.permissions import BasePermission


class HasLitrixPermission(BasePermission):
    required_perm = None

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        perm = getattr(view, 'required_perm', None) or self.required_perm
        if not perm:
            return True
        return request.user.has_litrix_perm(perm)


def require_perm(code):
    class Permission(HasLitrixPermission):
        required_perm = code
    return Permission
