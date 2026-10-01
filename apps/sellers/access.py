"""Who is acting as a seller.

A user acts as a seller when linked to an ACTIVE `Seller`, either as its owner
(`Seller.user`: all its positions, its money) or as the manager of one of its positions
(`ServicePosition.manager`: only that position's goods, sales, stock and bookings). These
links, not a role, grant access.
"""

from rest_framework.permissions import BasePermission

from .models import Seller, SellerStatus, ServicePosition

_CACHE_ATTR = "_acting_seller"


def _acting(user) -> tuple[Seller | None, frozenset[int] | None]:
    """(seller, positions): positions is None for the seller's owner (all positions), else
    the ids of the active positions the user manages."""
    if not getattr(user, "is_authenticated", False) or not getattr(user, "pk", None):
        return None, None
    if not hasattr(user, _CACHE_ATTR):
        seller = Seller.objects.filter(user=user, status=SellerStatus.ACTIVE).first()
        positions = None
        if seller is None:
            managed = list(
                ServicePosition.objects.filter(
                    manager=user, seller__status=SellerStatus.ACTIVE
                ).select_related("seller")
            )
            if managed:
                seller = managed[0].seller
                positions = frozenset(p.pk for p in managed if p.seller_id == seller.pk)
        setattr(user, _CACHE_ATTR, (seller, positions))
    return getattr(user, _CACHE_ATTR)


def acting_seller(user) -> Seller | None:
    """The seller the user works for: as its owner or as a position manager."""
    return _acting(user)[0]


def acting_positions(user) -> frozenset[int] | None:
    """None: no position limit (owner, or not a seller). Else: the managed position ids."""
    return _acting(user)[1]


def owns_seller(user) -> Seller | None:
    """The seller whose owner the user is (seller-wide data such as money)."""
    seller, positions = _acting(user)
    return seller if positions is None else None


class CatalogPermission(BasePermission):
    """Global RBAC permission for the action, or ownership for a seller's own objects.

    Views declare:
      required_permissions  {action: [codenames]} for global access (as HasPermissions)
      seller_actions        actions an active seller may perform on their own objects
      scope_permission      codename that lets a user list everyone's objects
      owner_seller_id(obj)  the seller id that owns `obj`
      owner_position_id(obj)  the position of `obj`, for views open to position managers;
                            views without it are seller-wide (owner only)
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
        if view.action not in getattr(view, "seller_actions", ()):
            return False
        seller, positions = _acting(user)
        if seller is None:
            return False
        if positions is not None:
            # Position managers: only views about positions, minus owner-only actions.
            return hasattr(view, "owner_position_id") and view.action not in getattr(
                view, "owner_only_actions", ()
            )
        return True

    def has_object_permission(self, request, view, obj) -> bool:
        if self._has_global(request, view):
            return True
        seller, positions = _acting(request.user)
        if seller is None or view.owner_seller_id(obj) != seller.pk:
            return False
        return positions is None or view.owner_position_id(obj) in positions


class SellerScopedQuerysetMixin:
    """Users without `scope_permission` only see their own seller's objects."""

    seller_lookup = "seller"

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if user.has_rbac_perm(self.scope_permission):
            return qs
        seller, positions = _acting(user)
        if seller is None:
            return qs.none()
        qs = qs.filter(**{self.seller_lookup: seller})
        if positions is not None:
            lookup = getattr(self, "position_lookup", None)
            return qs.filter(**{f"{lookup}__in": positions}) if lookup else qs.none()
        return qs

    def get_serializer_context(self):
        context = super().get_serializer_context()
        user = self.request.user
        # Sellers without global rights are pinned to their own seller.
        unrestricted = user.is_authenticated and user.has_rbac_perm(self.scope_permission)
        context["own_seller"] = None if unrestricted else acting_seller(user)
        # Position managers are also pinned to their positions.
        context["own_positions"] = None if unrestricted else acting_positions(user)
        return context
