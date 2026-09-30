from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed

from .models import RFIDDevice


class DevicePrincipal:
    """`request.user` for a reader. It holds no RBAC permissions."""

    is_authenticated = True
    is_anonymous = False
    is_active = True
    pk = None
    rbac_permissions = frozenset()

    def __init__(self, device: RFIDDevice):
        self.device = device

    def has_rbac_perms(self, codenames) -> bool:
        return False

    def __str__(self):
        return f"device:{self.device.code}"


class DeviceAuthentication(BaseAuthentication):
    """`Authorization: Device <api key>`."""

    keyword = "Device"

    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if not parts or parts[0].lower() != self.keyword.lower().encode():
            return None
        if len(parts) != 2:
            raise AuthenticationFailed("Invalid device credentials.")
        key = parts[1].decode(errors="ignore")
        for device in RFIDDevice.objects.filter(api_key_prefix=key[:8], is_active=True):
            if device.check_api_key(key):
                return DevicePrincipal(device), device
        raise AuthenticationFailed("Invalid device credentials.")

    def authenticate_header(self, request):
        return self.keyword


class DeviceAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = DeviceAuthentication
    name = "deviceKey"

    def get_security_definition(self, auto_schema):
        return {
            "type": "apiKey",
            "in": "header",
            "name": "Authorization",
            "description": "RFID reader key, sent as `Device <api key>`.",
        }
