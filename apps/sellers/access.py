"""Who is acting as a seller.

A user acts as a seller when linked to an ACTIVE `Seller`. That link, not a role, is
what grants access to the seller's own catalogue.
"""

from rest_framework.permissions import BasePermission

from .models import Seller, SellerStatus

_CACHE_ATTR = "_acting_seller"


def acting_seller(user) -> Seller | None:
    if not getattr(user, "is_authenticated", False) or not getattr(user, "pk", None):
        return None
    if not hasattr(user, _CACHE_ATTR):
        seller = Seller.objects.filter(user=user, status=SellerStatus.ACTIVE).first()
        setattr(user, _CACHE_ATTR, seller)
    return getattr(user, _CACHE_ATTR)


class CatalogPermission(BasePermission):
    """Global RBAC permission for the action, or ownership for a seller's own objects.

    Views declare:
      required_permissions  {action: [codenames]} for global access (as HasPermissions)
      seller_actions        actions an active seller may perform on their own objects
      scope_permission      codename that lets a user list everyone's objects
      owner_seller_id(obj)  the seller id that owns `obj`
    """

    def _has_global(self, request, view) -> bool:
        perms = getattr(view, "required_permissions", {}).get(view.action)
        return perms is not None and request.user.has_rbac_perms(perms)

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if request.method == "OPTIONS":
            return True
        if self._has_global(request, view):
            return True
        return view.action in getattr(view, "seller_actions", ()) and bool(acting_seller(user))

    def has_object_permission(self, request, view, obj) -> bool:
        if self._has_global(request, view):
            return True
        seller = acting_seller(request.user)
        return seller is not None and view.owner_seller_id(obj) == seller.pk


class SellerScopedQuerysetMixin:
    """Users without `scope_permission` only see their own seller's objects."""

    seller_lookup = "seller"

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if user.has_rbac_perm(self.scope_permission):
            return qs
        seller = acting_seller(user)
        if seller is None:
            return qs.none()
        return qs.filter(**{self.seller_lookup: seller})

    def get_serializer_context(self):
        context = super().get_serializer_context()
        user = self.request.user
        # Sellers without global rights are pinned to their own seller.
        context["own_seller"] = (
            None
            if user.is_authenticated and user.has_rbac_perm(self.scope_permission)
            else acting_seller(user)
        )
        return context
