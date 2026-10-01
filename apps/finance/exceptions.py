from django.utils.translation import gettext_lazy as _
from rest_framework import status

from common.exceptions import DomainError


class InsufficientBalance(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "INSUFFICIENT_BALANCE"
    default_detail = _("Developer account has insufficient balance.")


class AccountNotActive(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "ACCOUNT_NOT_ACTIVE"
    default_detail = _("This account cannot be used for this operation in its current status.")


class DepositLimitExceeded(DomainError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "DEPOSIT_LIMIT_EXCEEDED"
    default_detail = _("The amount exceeds the maximum allowed for a single deposit.")


class SelfTransactionForbidden(DomainError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "SELF_TRANSACTION_FORBIDDEN"
    default_detail = _("You cannot deposit to or adjust your own account.")


class IdempotencyKeyReused(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "IDEMPOTENCY_KEY_REUSED"
    default_detail = _("This Idempotency-Key was already used for a different request.")


class InvalidAccountTransition(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "INVALID_ACCOUNT_TRANSITION"
    default_detail = _("The account cannot move to this status from its current status.")


class PinNotSet(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "PIN_NOT_SET"
    default_detail = _("The developer has not set a purchase PIN yet.")


class InvalidPin(DomainError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INVALID_PIN"
    default_detail = _("The PIN is incorrect.")


class PinLocked(DomainError):
    status_code = status.HTTP_423_LOCKED
    code = "PIN_LOCKED"
    default_detail = _("Too many wrong PIN attempts. Try again later.")
