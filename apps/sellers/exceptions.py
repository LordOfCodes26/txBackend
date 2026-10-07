from django.utils.translation import gettext_lazy as _
from rest_framework import status

from common.exceptions import DomainError


class SellerProfileNotFound(DomainError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "SELLER_PROFILE_NOT_FOUND"
    default_detail = _("Your account is not linked to a seller.")


class PositionHasGoods(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "POSITION_HAS_GOODS"
    default_detail = _("Move or delete this counter's goods before deleting it.")
