from django.utils.translation import gettext_lazy as _
from rest_framework import status

from common.exceptions import DomainError


class PrivilegeEscalation(DomainError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "PRIVILEGE_ESCALATION"
    default_detail = _("You cannot grant or manage access beyond your own permissions.")


class LastAdmin(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "LAST_ADMIN"
    default_detail = _("At least one active user must keep the ADMIN role.")


class RoleAlreadyAssigned(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "ROLE_ALREADY_ASSIGNED"
    default_detail = _("The user already has this role.")


class RoleNotAssigned(DomainError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "ROLE_NOT_ASSIGNED"
    default_detail = _("The user does not have this role.")


class RoleLocked(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "ROLE_LOCKED"
    default_detail = _("The Admin role always has every permission.")


class SystemRole(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "SYSTEM_ROLE"
    default_detail = _("Built-in roles cannot be deleted.")


class RoleInUse(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "ROLE_IN_USE"
    default_detail = _("Take this role away from its users before deleting it.")
