"""Which stores' cash a user sees: `cash.view` every store (in their buildings); a store's
own login (its owner) that store."""

from django.db.models import Q

from apps.rfid.scope import building_scope, sellers_with_positions_in
from apps.sellers.access import GOODS, acting_positions, acting_seller, owns_seller, seller_areas
from apps.sellers.models import Seller


def visible_sellers(user):
    if user.has_rbac_perm("cash.view"):
        scope = building_scope(user, "cash.view")
        if scope is None:
            return Seller.objects.all()
        return Seller.objects.filter(pk__in=sellers_with_positions_in(scope).values("pk"))
    own = owns_seller(user)
    if own is None or GOODS not in seller_areas(user):
        return Seller.objects.none()
    return Seller.objects.filter(pk=own.pk)


def sellers_for(user, permission: str):
    """The stores on which the user may use a cash control (in their buildings)."""
    if not user.has_rbac_perm(permission):
        return Seller.objects.none()
    scope = building_scope(user, permission)
    if scope is None:
        return Seller.objects.all()
    return Seller.objects.filter(pk__in=sellers_with_positions_in(scope).values("pk"))


def visible_requests(user, qs):
    """The requests of the stores the user sees, and everyone's own."""
    return qs.filter(Q(seller__in=visible_sellers(user)) | Q(requested_by=user))


def sellable_purchases(user, qs):
    """The sales the user may ask to delete: a store's own people theirs; staff every one."""
    if user.has_rbac_perm("purchase.delete") or user.has_rbac_perm("cash.approve"):
        permission = "purchase.delete" if user.has_rbac_perm("purchase.delete") else "cash.approve"
        scope = building_scope(user, permission)
        return qs if scope is None else qs.filter(service_position__building__in=scope)
    seller = acting_seller(user)
    if seller is None or GOODS not in seller_areas(user):
        return qs.none()
    qs = qs.filter(seller=seller)
    positions = acting_positions(user)
    return qs if positions is None else qs.filter(service_position__in=positions)
