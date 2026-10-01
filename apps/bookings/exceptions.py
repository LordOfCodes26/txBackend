from rest_framework import status

from common.exceptions import DomainError


class RentalNotAvailable(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "RENTAL_NOT_AVAILABLE"
    default_detail = "This rental is not available for booking."


class InvalidSlot(DomainError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INVALID_SLOT"
    default_detail = "This time is not a bookable slot."


class SlotUnavailable(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "SLOT_UNAVAILABLE"
    default_detail = "This time is already booked."
