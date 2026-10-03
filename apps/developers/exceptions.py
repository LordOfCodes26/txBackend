from django.utils.translation import gettext_lazy as _
from rest_framework import status

from common.exceptions import DomainError


class DeveloperProfileNotFound(DomainError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "DEVELOPER_PROFILE_NOT_FOUND"
    default_detail = _("Your account is not linked to a developer profile.")
