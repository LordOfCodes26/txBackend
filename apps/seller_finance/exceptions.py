from django.utils.translation import gettext_lazy as _
from rest_framework import status

from common.exceptions import DomainError


class InsufficientSellerBalance(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "INSUFFICIENT_SELLER_BALANCE"
    default_detail = _("The seller's available balance is too low.")


class InvalidPayoutTransition(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "INVALID_PAYOUT_TRANSITION"
    default_detail = _("The payout cannot move to this status from its current status.")


class SelfApprovalForbidden(DomainError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "SELF_APPROVAL_FORBIDDEN"
    default_detail = _("A payout must be approved by someone other than its requester.")


class OwnSellerForbidden(DomainError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "OWN_SELLER_FORBIDDEN"
    default_detail = _("You cannot approve, pay or adjust your own seller's money.")
