from django.utils.translation import gettext_lazy as _
from rest_framework import status

from common.exceptions import DomainError


class InsufficientStock(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "INSUFFICIENT_STOCK"
    default_detail = _("Not enough stock for this change.")


class StockNotTracked(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "STOCK_NOT_TRACKED"
    default_detail = _("This good does not track stock.")


class NoStockChange(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "NO_STOCK_CHANGE"
    default_detail = _("The counted quantity equals the current stock; nothing to adjust.")


class TooManyImages(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "TOO_MANY_IMAGES"
    default_detail = _("This good already has the maximum number of images.")
