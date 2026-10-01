"""Building-scoped access for the BUILDING_MANAGER role.

A building manager is linked to one or more buildings (`Building.managers`). For a
permission that the user holds only through BUILDING_MANAGER, every list is narrowed to
those buildings and every change must stay inside them. If any other role of the user
grants the same permission (e.g. MANAGER), that permission is unrestricted.
"""

from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError

SCOPED_ROLE = "BUILDING_MANAGER"
_CACHE_ATTR = "_building_scopes"


def building_scope(user, permission: str) -> frozenset[int] | None:
    """None: no building limit for `permission`. Else the ids of the user's buildings
    (empty when they manage none: they see nothing)."""
    from apps.accounts.models import Role

    if not getattr(user, "is_authenticated", False) or user.is_superuser:
        return None
    cache = user.__dict__.setdefault(_CACHE_ATTR, {})
    if permission not in cache:
        roles = Role.objects.filter(user_roles__user=user, permissions__codename=permission)
        if not roles.filter(code=SCOPED_ROLE).exists() or roles.exclude(code=SCOPED_ROLE).exists():
            cache[permission] = None
        else:
            cache[permission] = frozenset(user.managed_buildings.values_list("pk", flat=True))
    return cache[permission]


def ensure_in_scope(user, permission: str, building_id, field: str = "building") -> None:
    """Reject a change that would put data outside the user's buildings."""
    scope = building_scope(user, permission)
    if scope is not None and building_id not in scope:
        raise ValidationError({field: [_("You can only manage data of your own building.")]})


class BuildingScopedMixin:
    """Narrow a view's queryset to the user's buildings for `building_permission`.

    Views set `building_lookup` (path to the building, e.g. "developer__building") and
    `building_permission` (default: the first permission the action requires).
    """

    building_lookup: str = "building"
    building_permission: str | None = None

    def scope_permission_for_action(self) -> str | None:
        if self.building_permission:
            return self.building_permission
        perms = getattr(self, "required_permissions", {}).get(getattr(self, "action", None))
        if perms is None:
            perms = getattr(self, "required_permissions", {}).get(self.request.method.lower())
        return next(iter(perms), None) if perms else None

    def buildings(self) -> frozenset[int] | None:
        permission = self.scope_permission_for_action()
        return building_scope(self.request.user, permission) if permission else None

    def get_queryset(self):
        qs = super().get_queryset()
        scope = self.buildings()
        if scope is None:
            return qs
        return self.filter_by_buildings(qs, scope)

    def filter_by_buildings(self, qs, scope):
        return qs.filter(**{f"{self.building_lookup}__in": scope})
