from django.utils.translation import gettext_lazy as _
from rest_framework import status

from common.exceptions import DomainError


class CashConflict(DomainError):
    status_code = status.HTTP_409_CONFLICT


class CashNotAccepted(CashConflict):
    code = "CASH_NOT_ACCEPTED"
    default_detail = _("This store does not take cash. Pay with a card.")


class CashSaleFixed(CashConflict):
    code = "CASH_SALE_FIXED"
    default_detail = _("A cash sale can't be changed. Ask finance to delete it and sell again.")


class CashDeletedByFinance(CashConflict):
    code = "CASH_DELETED_BY_FINANCE"
    default_detail = _("Only finance can delete a cash sale. Tell them why.")


class CashStoreLocked(DomainError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "CASH_STORE_LOCKED"
    default_detail = _("Your store takes cash, so someone else must do this. Ask finance.")


class OwnStore(DomainError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "OWN_STORE"
    default_detail = _("You work for this store, so someone else must do this.")


class OwnSale(DomainError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "OWN_SALE"
    default_detail = _("You took the cash for this sale, so someone else must delete it.")


class NotReversible(CashConflict):
    code = "NOT_REVERSIBLE"
    default_detail = _("Only a handover or a charge can be cancelled, and only once.")


class StockMoved(CashConflict):
    code = "STOCK_MOVED"
    default_detail = _("Some of these goods were sold or moved meanwhile. Count them again.")


class CountResolved(CashConflict):
    code = "COUNT_RESOLVED"
    default_detail = _("This stock count has nothing left to explain.")
