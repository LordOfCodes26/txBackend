"""Who is acting as a seller, and for which part of the store.

A user acts as a seller when they have a seller role AND are linked to an ACTIVE
`Seller`, either as its owner
(`Seller.user`: all its positions, its money) or as the manager of one of its positions
(`ServicePosition.manager`: only that position's goods, sales, stock and bookings). These
links grant access to the store; the seller roles say which part of it (`seller_areas`):

  PLAYGROUND_SELLER  "playground": courts (RENTAL goods), bookings, the playground desk
                     and booking payments
  GOODS_SELLER       "goods": goods and services, stock, the till and its sales

Views declare `seller_area` ("goods" / "playground") when they belong to one part, or
narrow mixed querysets with `seller_kinds` / `seller_purchase_kinds`.

The playground is shared, not a store's own: every PLAYGROUND_SELLER works with every
court, booking and booking payment, linked to a store or not (`works_playground`). Views
mark those rows with `playground_filter` (a Q; `Q()` = every row of a playground view).
"""

from django.db.models import Q
from rest_framework.permissions import BasePermission

from .models import Seller, SellerStatus, ServicePosition

_CACHE_ATTR = "_acting_seller"
_AREAS_ATTR = "_seller_areas"

GOODS = "goods"
PLAYGROUND = "playground"


def _acting(user) -> tuple[Seller | None, frozenset[int] | None]:
    """(seller, positions): positions is None for the seller's owner (all positions), else
    the ids of the active positions the user manages."""
    if not getattr(user, "is_authenticated", False) or not getattr(user, "pk", None):
        return None, None
    if not hasattr(user, _CACHE_ATTR):
        seller, positions = None, None
        if has_seller_role(user):
            seller = Seller.objects.filter(user=user, status=SellerStatus.ACTIVE).first()
        if seller is None and has_seller_role(user):
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


def seller_areas(user) -> frozenset[str]:
    """The parts of a store the user's seller roles cover: {"goods", "playground"}."""
    if not getattr(user, "is_authenticated", False) or not getattr(user, "pk", None):
        return frozenset()
    if not hasattr(user, _AREAS_ATTR):
        from apps.accounts.models import UserRole
        from apps.accounts.rbac import SELLER_AREAS

        codes = UserRole.objects.filter(user=user, role__code__in=SELLER_AREAS).values_list(
            "role__code", flat=True
        )
        setattr(user, _AREAS_ATTR, frozenset(SELLER_AREAS[c] for c in codes))
    return getattr(user, _AREAS_ATTR)


def has_seller_role(user) -> bool:
    return bool(seller_areas(user))


def works_playground(user) -> bool:
    """A playground seller: every court and booking, whatever store (if any) they run."""
    return PLAYGROUND in seller_areas(user)


def _shared(view, user) -> Q | None:
    """The playground rows of `view` the user works with as a playground seller."""
    rows = getattr(view, "playground_filter", None)
    return rows if rows is not None and works_playground(user) else None


def seller_kinds(user) -> set[str]:
    """The goods kinds a seller works with: courts for the playground, the rest for goods."""
    from apps.goods.models import GoodKind

    areas = seller_areas(user)
    kinds = set()
    if PLAYGROUND in areas:
        kinds.add(GoodKind.RENTAL)
    if GOODS in areas:
        kinds |= {GoodKind.PRODUCT, GoodKind.SERVICE}
    return kinds


def seller_purchase_kinds(user) -> set[str]:
    """The purchases a seller works with: booking payments for the playground, till sales
    for goods."""
    from apps.purchases.models import PurchaseKind

    areas = seller_areas(user)
    kinds = set()
    if PLAYGROUND in areas:
        kinds.add(PurchaseKind.BOOKING)
    if GOODS in areas:
        kinds.add(PurchaseKind.SALE)
    return kinds


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
      seller_area           GOODS or PLAYGROUND (or {action: area}): the seller role needed
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
        area = getattr(view, "seller_area", None)
        if isinstance(area, dict):  # per action, e.g. {"create": GOODS}
            area = area.get(view.action)
        if area in (None, PLAYGROUND) and _shared(view, user) is not None:
            return True  # the shared playground; objects are checked one by one
        seller, positions = _acting(user)
        if seller is None:
            return False
        if area is not None and area not in seller_areas(user):
            return False  # e.g. a goods seller at the playground desk
        if positions is not None:
            # Position managers: only views about positions, minus owner-only actions.
            return hasattr(view, "owner_position_id") and view.action not in getattr(
                view, "owner_only_actions", ()
            )
        return True

    def has_object_permission(self, request, view, obj) -> bool:
        if self._has_global(request, view):
            return True
        rows = _shared(view, request.user)
        if rows is not None and type(obj).objects.filter(rows, pk=obj.pk).exists():
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
        shared = _shared(self, user)
        if shared is None:
            return self._own_rows(qs, user)
        if not shared:  # Q(): every row (`Q(...) | Q()` would drop it)
            return qs
        return qs.filter(Q(pk__in=self._own_rows(qs, user).values("pk")) | shared)

    def _own_rows(self, qs, user):
        seller, positions = _acting(user)
        if seller is None:
            return qs.none()
        qs = qs.filter(**{self.seller_lookup: seller})
        qs = self.filter_seller_area(qs, user)
        if positions is not None:
            lookup = getattr(self, "position_lookup", None)
            return qs.filter(**{f"{lookup}__in": positions}) if lookup else qs.none()
        return qs

    def filter_seller_area(self, qs, user):
        """Views mixing both parts of a store narrow a seller's rows to their roles' part."""
        return qs

    def get_serializer_context(self):
        context = super().get_serializer_context()
        user = self.request.user
        # Sellers without global rights are pinned to their own seller; playground sellers
        # are not, on views that are all playground (bookings, the desk).
        unrestricted = user.is_authenticated and (
            user.has_rbac_perm(self.scope_permission) or self.unpinned(user)
        )
        context["own_seller"] = None if unrestricted else acting_seller(user)
        # Position managers are also pinned to their positions.
        context["own_positions"] = None if unrestricted else acting_positions(user)
        return context

    def unpinned(self, user) -> bool:
        """True: the request may use any store's rows (the shared playground)."""
        rows = getattr(self, "playground_filter", None)
        return rows is not None and not rows and works_playground(user)
