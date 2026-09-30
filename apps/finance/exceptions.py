from rest_framework import status

from common.exceptions import DomainError


class InsufficientBalance(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "INSUFFICIENT_BALANCE"
    default_detail = "Developer account has insufficient balance."


class AccountNotActive(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "ACCOUNT_NOT_ACTIVE"
    default_detail = "This account cannot be used for this operation in its current status."


class DepositLimitExceeded(DomainError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "DEPOSIT_LIMIT_EXCEEDED"
    default_detail = "The amount exceeds the maximum allowed for a single deposit."


class SelfTransactionForbidden(DomainError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "SELF_TRANSACTION_FORBIDDEN"
    default_detail = "You cannot deposit to or adjust your own account."


class IdempotencyKeyReused(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "IDEMPOTENCY_KEY_REUSED"
    default_detail = "This Idempotency-Key was already used for a different request."


class InvalidAccountTransition(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "INVALID_ACCOUNT_TRANSITION"
    default_detail = "The account cannot move to this status from its current status."
