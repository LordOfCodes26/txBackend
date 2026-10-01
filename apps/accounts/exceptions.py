from django.utils.translation import gettext_lazy as _
from rest_framework import status

from common.exceptions import DomainError


class PrivilegeEscalation(DomainError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "PRIVILEGE_ESCALATION"
    default_detail = _("You cannot grant or manage access beyond your own permissions.")


class LastBoss(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "LAST_BOSS"
    default_detail = _("At least one active user must keep the BOSS role.")


class RoleAlreadyAssigned(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "ROLE_ALREADY_ASSIGNED"
    default_detail = _("The user already has this role.")


class RoleNotAssigned(DomainError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "ROLE_NOT_ASSIGNED"
    default_detail = _("The user does not have this role.")
