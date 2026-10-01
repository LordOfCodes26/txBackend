import logging

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from django.db.models import ProtectedError, RestrictedError
from django.http import Http404
from django.utils.translation import gettext_lazy as _
from psycopg import errors as pg_errors
from rest_framework import exceptions, status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler
from rest_framework.views import set_rollback

logger = logging.getLogger(__name__)


class DomainError(exceptions.APIException):
    """Base class for business-rule violations.

    Subclasses set `code` (a stable, machine-readable string the frontend can switch on)
    and `default_detail`:

        class InsufficientBalance(DomainError):
            status_code = 409
            code = "INSUFFICIENT_BALANCE"
            default_detail = _("Developer account has insufficient balance.")
    """

    status_code = status.HTTP_400_BAD_REQUEST
    code = "DOMAIN_ERROR"
    default_detail = _("The request violates a business rule.")

    def __init__(self, message: str | None = None, *, details: dict | None = None):
        super().__init__(detail=message or self.default_detail)
        self.details = details


class Conflict(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "CONFLICT"
    default_detail = _("The request conflicts with existing data.")


class InUse(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "IN_USE"
    default_detail = _("This item is still in use and can't be deleted.")


def _error_body(code: str, message: str, details=None) -> dict:
    body = {"code": code, "message": message}
    if details:
        body["details"] = details
    return {"error": body}


def _normalize(exc):
    if isinstance(exc, Http404):
        return exceptions.NotFound()
    if isinstance(exc, DjangoPermissionDenied):
        return exceptions.PermissionDenied()
    if isinstance(exc, ProtectedError | RestrictedError):
        # Deleting something other rows still point to (e.g. a building with doors).
        return InUse()
    if isinstance(exc, IntegrityError) and isinstance(exc.__cause__, pg_errors.UniqueViolation):
        # Serializers validate uniqueness first; this covers the race between two requests.
        return Conflict(details={"constraint": exc.__cause__.diag.constraint_name})
    if isinstance(exc, DjangoValidationError):
        detail = exc.message_dict if hasattr(exc, "error_dict") else exc.messages
        return exceptions.ValidationError(detail=detail)
    return exc


def exception_handler(exc, context):
    """Render every API error as {"error": {"code", "message", "details?"}}."""
    exc = _normalize(exc)
    response = drf_exception_handler(exc, context)

    if response is None:
        logger.exception("Unhandled API error", exc_info=exc)
        set_rollback()
        return Response(
            _error_body("INTERNAL_ERROR", str(_("An unexpected error occurred."))),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    if isinstance(exc, DomainError):
        response.data = _error_body(exc.code, str(exc.detail), exc.details)
    elif isinstance(exc, exceptions.ValidationError):
        response.data = _error_body("VALIDATION_ERROR", str(_("Invalid input.")), exc.detail)
    else:
        detail = exc.detail
        if isinstance(detail, dict):  # e.g. simplejwt's InvalidToken
            message = str(detail.get("detail", ""))
            code = str(detail.get("code", exc.default_code))
        else:
            message = str(detail)
            code = getattr(detail, "code", None) or exc.default_code
        response.data = _error_body(code.upper(), message)
    return response
