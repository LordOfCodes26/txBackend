"""Cash sales: goods sold at a till to people without a card, at stores that accept cash
(`Seller.accepts_cash`). The seller keeps the cash; finance pays the store its card sales.
Selling is in apps.purchases; here is what only finance does."""

from django.utils.translation import gettext_lazy as _

from apps.purchases.models import Payment, Purchase
from apps.sellers.access import acting_seller
from apps.sellers.models import Seller

from .exceptions import CashSaleFixed, OwnSale, OwnStore


def works_for(user, seller_id: int) -> bool:
    """The store's own login, or the manager of one of its counters."""
    seller = acting_seller(user)
    if seller is not None and seller.pk == seller_id:
        return True
    pk = getattr(user, "pk", None)
    return pk is not None and (
        Seller.objects.filter(pk=seller_id, user_id=pk).exists()
        or Seller.objects.filter(
            pk=seller_id, positions__manager_id=pk, positions__deleted_at__isnull=True
        ).exists()
    )


def delete_sale(*, actor, purchase: Purchase, reason: str) -> Purchase:
    """Finance deletes a cash sale, saying why: not for a store they work for, nor a sale they
    took the cash for."""
    from apps.purchases import services as purchases

    if purchase.payment != Payment.CASH:
        raise CashSaleFixed(_("This is not a cash sale."))
    if works_for(actor, purchase.seller_id):
        raise OwnStore()
    if purchase.confirmed_by_id == actor.pk:
        raise OwnSale()
    return purchases.delete_cash_sale(actor=actor, purchase=purchase, reason=reason.strip())
