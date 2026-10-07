from django.utils.translation import gettext_lazy as _
from rest_framework import status

from common.exceptions import DomainError


class RFIDConflict(DomainError):
    status_code = status.HTTP_409_CONFLICT


class CardNotActive(RFIDConflict):
    code = "CARD_NOT_ACTIVE"
    default_detail = _("Only active cards can be assigned.")


class CardAlreadyAssigned(RFIDConflict):
    code = "CARD_ALREADY_ASSIGNED"
    default_detail = _("This card is already assigned to a developer.")


class DeveloperAlreadyHasCard(RFIDConflict):
    code = "DEVELOPER_ALREADY_HAS_CARD"
    default_detail = _("This developer already has an active card. Use replace instead.")


class CardNotAssigned(RFIDConflict):
    code = "CARD_NOT_ASSIGNED"
    default_detail = _("This card is not assigned to anyone.")


class DeveloperNotAssignable(RFIDConflict):
    code = "DEVELOPER_NOT_ASSIGNABLE"
    default_detail = _("Cards cannot be assigned to terminated or deleted developers.")


class InvalidCardTransition(RFIDConflict):
    code = "INVALID_CARD_TRANSITION"
    default_detail = _("The card cannot move to this status from its current status.")


class NotATillReader(DomainError):
    code = "NOT_A_TILL_READER"
    default_detail = _("Only till readers are assigned to a seller.")
