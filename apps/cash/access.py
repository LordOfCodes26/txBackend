"""Which stores' cash a user sees: `cash.view` every store (in their buildings); a store's
own login (its owner) that store."""

from apps.rfid.scope import building_scope, sellers_with_positions_in
from apps.sellers.access import GOODS, owns_seller, seller_areas
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
