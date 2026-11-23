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


class CashDeleteNeedsApproval(CashConflict):
    code = "CASH_DELETE_NEEDS_APPROVAL"
    default_detail = _("Finance must approve deleting a cash sale. Send a request with a reason.")


class CashStoreLocked(DomainError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "CASH_STORE_LOCKED"
    default_detail = _("Your store takes cash, so someone else must do this. Ask finance.")


class OwnStore(DomainError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "OWN_STORE"
    default_detail = _("You work for this store, so someone else must do this.")


class OwnRequest(DomainError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "OWN_REQUEST"
    default_detail = _("You can't approve your own request or your own sale.")


class RequestClosed(CashConflict):
    code = "REQUEST_CLOSED"
    default_detail = _("This request was already decided.")


class AlreadyRequested(CashConflict):
    code = "ALREADY_REQUESTED"
    default_detail = _("This is already waiting for approval.")


class NotReversible(CashConflict):
    code = "NOT_REVERSIBLE"
    default_detail = _("Only a handover or a charge can be cancelled, and only once.")


class StockMoved(CashConflict):
    code = "STOCK_MOVED"
    default_detail = _("Some of these goods were sold or moved meanwhile. Count them again.")


class CountResolved(CashConflict):
    code = "COUNT_RESOLVED"
    default_detail = _("This stock count has nothing left to explain.")


class AwaitingApproval(CashConflict):
    code = "AWAITING_APPROVAL"
    default_detail = _("Finance must approve this good's price before it goes on sale.")
