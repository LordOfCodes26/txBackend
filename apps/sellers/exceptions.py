from rest_framework import status

from common.exceptions import DomainError


class SellerProfileNotFound(DomainError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "SELLER_PROFILE_NOT_FOUND"
    default_detail = "Your account is not linked to a seller."


class PositionHasGoods(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "POSITION_HAS_GOODS"
    default_detail = "Move or delete this position's goods before deleting it."
