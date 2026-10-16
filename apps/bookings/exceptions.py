from django.utils.translation import gettext_lazy as _
from rest_framework import status

from common.exceptions import DomainError


class RentalNotAvailable(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "RENTAL_NOT_AVAILABLE"
    default_detail = _("This rental is not available for booking.")


class InvalidSlot(DomainError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INVALID_SLOT"
    default_detail = _("This time is not a bookable slot.")


class SlotUnavailable(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "SLOT_UNAVAILABLE"
    default_detail = _("This time is already booked.")


class AlreadyBookedThen(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "ALREADY_BOOKED_THEN"
    default_detail = _("You already have a booking at this time.")


class BookingStarted(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "BOOKING_STARTED"
    default_detail = _("This booking has already started and can't be changed.")


class BookingCheckedIn(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "BOOKING_CHECKED_IN"
    default_detail = _("The developer has already checked in to this booking.")
