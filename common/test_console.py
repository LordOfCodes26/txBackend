"""Staging-only test console: a page to exercise door attendance and the till end to end.

Enabled by TEST_CONSOLE_ENABLED (off by default; never enable it in production).
The page itself is static; it talks to the normal API. Browsers can't open raw TCP
connections, so door scans go through `door_scan` below, which skips only the transport
authentication (fixed IP) and otherwise runs the same validation and pipeline as a real
door. Till taps from the page use the real device endpoint with SN + ID.
"""

from django.conf import settings
from django.http import Http404
from django.shortcuts import render
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.rfid import services
from apps.rfid.models import DevicePurpose, RFIDDevice
from apps.rfid.serializers import ScanResponseSerializer, ScanSerializer
from common.permissions import HasPermissions


def _ensure_enabled():
    if not settings.TEST_CONSOLE_ENABLED:
        raise Http404()


def console_page(request):
    _ensure_enabled()
    return render(request, "common/test_console.html")


class SimulatedDoorScanView(APIView):
    """Simulate a door scan as if `door` had sent `{"ID", "Type", "UID"}` over TCP."""

    permission_classes = [HasPermissions]
    required_permissions = {"post": ["rfid.device.manage"]}

    @extend_schema(
        request=inline_serializer(
            "SimulatedDoorScan",
            {
                "door": serializers.CharField(),
                "type": serializers.CharField(),
                "uid": serializers.CharField(),
            },
        ),
        responses=ScanResponseSerializer,
    )
    def post(self, request):
        _ensure_enabled()
        code = str(request.data.get("door", "")).strip()
        device = RFIDDevice.objects.filter(
            code__iexact=code, purpose=DevicePurpose.ATTENDANCE, is_active=True
        ).first()
        if device is None:
            raise NotFound("No active door with this code.")
        payload = {
            "ID": device.code,
            "Type": request.data.get("type"),
            "UID": request.data.get("uid"),
        }
        serializer = ScanSerializer(data=payload, context={"device": device})
        serializer.is_valid(raise_exception=True)
        event, _ = services.record_scan(
            device=device,
            uid=serializer.validated_data["uid"],
            direction=serializer.validated_data["direction"],
        )
        return Response(ScanResponseSerializer(event).data, status=201)
