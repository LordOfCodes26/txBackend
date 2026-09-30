from rest_framework import status

from common.exceptions import DomainError


class PurchaseConflict(DomainError):
    status_code = status.HTTP_409_CONFLICT


class PurchaseNotDraft(PurchaseConflict):
    code = "PURCHASE_NOT_DRAFT"
    default_detail = "Only draft purchases can be changed."


class PurchaseEmpty(PurchaseConflict):
    code = "PURCHASE_EMPTY"
    default_detail = "Add at least one item before confirming."


class GoodNotAvailable(PurchaseConflict):
    code = "GOOD_NOT_AVAILABLE"
    default_detail = "This good is not available for sale."


class SellerNotActive(PurchaseConflict):
    code = "SELLER_NOT_ACTIVE"
    default_detail = "The seller or service position is not active."


class CardNotUsable(PurchaseConflict):
    code = "CARD_NOT_USABLE"
    default_detail = "This card cannot be used for purchases."


class DeveloperNotActive(PurchaseConflict):
    code = "DEVELOPER_NOT_ACTIVE"
    default_detail = "The card holder is not an active developer."


class SelfPurchaseForbidden(DomainError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "SELF_PURCHASE_FORBIDDEN"
    default_detail = "Sellers cannot charge their own card."
