from django.utils.translation import gettext_lazy as _
from rest_framework import status

from common.exceptions import DomainError


class DeveloperHasReports(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "DEVELOPER_HAS_REPORTS"
    default_detail = _("Reassign this developer's direct reports before deleting them.")


class DeveloperProfileNotFound(DomainError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "DEVELOPER_PROFILE_NOT_FOUND"
    default_detail = _("Your account is not linked to a developer profile.")
