from rest_framework.permissions import BasePermission


class HasPermissions(BasePermission):
    """RBAC check driven by the view's `required_permissions` mapping.

    Keys are viewset actions (`list`, `create`, custom `@action` names) or, for plain
    APIViews, lower-case HTTP methods. Values are iterables of permission codenames that
    are all required. An empty iterable means "any authenticated user".

    Anything not listed is denied, so a new endpoint is closed until someone decides
    who may use it.
    """

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not (user and user.is_authenticated):
            return False
        mapping = getattr(view, "required_permissions", None) or {}
        key = getattr(view, "action", None) or request.method.lower()
        if key == "metadata" or request.method == "OPTIONS":
            return True
        if key not in mapping:
            return False
        return user.has_rbac_perms(mapping[key])
