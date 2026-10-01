from django.conf import settings
from django.core.cache import cache
from django.utils.translation import gettext_lazy as _
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed

from common.middleware import client_ip

from .models import DevicePurpose, RFIDDevice


def device_for_key(key: str) -> RFIDDevice | None:
    """The active device owning this API key, or None."""
    for device in RFIDDevice.objects.filter(api_key_prefix=key[:8], is_active=True):
        if device.check_api_key(key):
            return device
    return None


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
            raise AuthenticationFailed(_("Invalid device credentials."))
        device = device_for_key(parts[1].decode(errors="ignore"))
        if device is None:
            raise AuthenticationFailed(_("Invalid device credentials."))
        return DevicePrincipal(device), device

    def authenticate_header(self, request):
        return self.keyword


class DeviceIPAuthentication(BaseAuthentication):
    """Key-less authentication for door devices that cannot send headers.

    Used only when the request has no Authorization header. The request must come from
    the device's registered `allowed_ip` AND name the device in its body (`ID`, as the
    doors send it, or `device_id`). Only active ATTENDANCE devices can be authenticated
    this way. The client IP is taken from the trusted reverse proxy (see
    common.middleware.client_ip), never from a client-supplied header.
    """

    def authenticate(self, request):
        if get_authorization_header(request) or request.method != "POST":
            return None
        code = _device_code_from_body(request)
        if not code:
            return None
        ip = client_ip(request)
        device = (
            RFIDDevice.objects.filter(
                code__iexact=code,
                allowed_ip=ip,
                is_active=True,
                purpose=DevicePurpose.ATTENDANCE,
            ).first()
            if ip
            else None
        )
        if device is None:
            raise AuthenticationFailed(_("No door device with this ID is registered for this IP."))
        return DevicePrincipal(device), device

    def authenticate_header(self, request):
        return "Device"


def _device_code_from_body(request) -> str:
    try:
        data = request.data
    except Exception:  # unparsable body: let the view report it
        return ""
    if not hasattr(data, "items"):
        return ""
    for key, value in data.items():
        if str(key).lower() in ("id", "device_id") and isinstance(value, str):
            return value.strip()
    return ""


class SNLockedOut(Exception):
    pass


def _fail_key(ip: str) -> str:
    return f"rfid-sn-fail:{ip}"


def till_for_sn(code: str, sn: str, ip: str | None) -> RFIDDevice | None:
    """The active TILL device named `code` whose serial number is `sn`, else None.

    Serial numbers are not secret-grade (often printed on the device), so repeated
    failures from one address lock that address out (RFID_SN_MAX_FAILURES within
    RFID_SN_LOCKOUT_SECONDS) to stop guessing.
    """
    key = _fail_key(ip or "unknown")
    if cache.get(key, 0) >= settings.RFID_SN_MAX_FAILURES:
        raise SNLockedOut()
    device = RFIDDevice.objects.filter(
        code__iexact=code, is_active=True, purpose=DevicePurpose.TILL
    ).first()
    if device is not None and device.check_sn(sn):
        return device
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=settings.RFID_SN_LOCKOUT_SECONDS)
    return None


def _body_value(request, *names: str) -> str:
    try:
        data = request.data
    except Exception:  # unparsable body: let the view report it
        return ""
    if not hasattr(data, "items"):
        return ""
    for key, value in data.items():
        if str(key).lower() in names and isinstance(value, str | int):
            return str(value).strip()
    return ""


class DeviceSNAuthentication(BaseAuthentication):
    """Key-less authentication for till readers: the body carries `SN` (the reader's serial
    number) and `ID` (its code). Used only when there is no Authorization header."""

    def authenticate(self, request):
        if get_authorization_header(request) or request.method != "POST":
            return None
        sn = _body_value(request, "sn")
        if not sn:
            return None
        code = _body_value(request, "id", "device_id")
        try:
            device = till_for_sn(code, sn, client_ip(request))
        except SNLockedOut as exc:
            raise AuthenticationFailed(_("Too many failed attempts; try again later.")) from exc
        if device is None:
            raise AuthenticationFailed(_("Unknown till reader ID or serial number."))
        return DevicePrincipal(device), device

    def authenticate_header(self, request):
        return "Device"


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
