from django.utils.translation import gettext_lazy as _
from rest_framework import status

from common.exceptions import DomainError


class PurchaseConflict(DomainError):
    status_code = status.HTTP_409_CONFLICT


class PurchaseNotDraft(PurchaseConflict):
    code = "PURCHASE_NOT_DRAFT"
    default_detail = _("Only draft purchases can be changed.")


class PurchaseEmpty(PurchaseConflict):
    code = "PURCHASE_EMPTY"
    default_detail = _("Add at least one item before confirming.")


class GoodNotAvailable(PurchaseConflict):
    code = "GOOD_NOT_AVAILABLE"
    default_detail = _("This good is not available for sale.")


class SellerNotActive(PurchaseConflict):
    code = "SELLER_NOT_ACTIVE"
    default_detail = _("The seller or counter is not active.")


class CardNotUsable(PurchaseConflict):
    code = "CARD_NOT_USABLE"
    default_detail = _("This card cannot be used for purchases.")


class DeveloperNotActive(PurchaseConflict):
    code = "DEVELOPER_NOT_ACTIVE"
    default_detail = _("The card holder is not an active developer.")


class SelfPurchaseForbidden(DomainError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "SELF_PURCHASE_FORBIDDEN"
    default_detail = _("Sellers cannot charge their own card.")


class CardNotPresented(PurchaseConflict):
    code = "CARD_NOT_PRESENTED"
    default_detail = _("Ask the developer to tap their card on this counter's reader.")


class ManualCardEntryDisabled(DomainError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "MANUAL_CARD_ENTRY_DISABLED"
    default_detail = _(
        "Typed card numbers are not accepted; the card must be tapped on the reader."
    )


class NoTillReader(PurchaseConflict):
    code = "NO_TILL_READER"
    default_detail = _("Choose one of the seller's till readers first.")


class PurchaseNotPaid(PurchaseConflict):
    code = "PURCHASE_NOT_PAID"
    default_detail = _("Only paid purchases can be changed or deleted.")


class PaidBookingNotEditable(PurchaseConflict):
    code = "PAID_BOOKING_NOT_EDITABLE"
    default_detail = _(
        "A paid court booking can't be edited here: move it to another time, or delete it."
    )


class PinRequired(DomainError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "PIN_REQUIRED"
    default_detail = _(
        "The new total is higher: the developer taps their card and enters their PIN to pay "
        "the difference."
    )
