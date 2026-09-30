from rest_framework.permissions import BasePermission

from .models import RFIDDevice


class IsRFIDDevice(BasePermission):
    def has_permission(self, request, view) -> bool:
        return isinstance(request.auth, RFIDDevice)
