from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.views import APIView

from common.context import get_request_context
from common.permissions import HasPermissions

from . import services
from .authentication import (
    DeviceAuthentication,
    DeviceIPAuthentication,
    DeviceSNAuthentication,
)
from .filters import (
    RFIDCardAssignmentFilter,
    RFIDCardFilter,
    RFIDDeviceFilter,
    RFIDEventFilter,
)
from .models import (
    Building,
    DevicePurpose,
    RFIDCard,
    RFIDCardAssignment,
    RFIDDevice,
    RFIDEvent,
)
from .permissions import IsRFIDDevice
from .serializers import (
    BatchResultSerializer,
    BatchScanSerializer,
    BuildingSerializer,
    CardAssignSerializer,
    CardReasonSerializer,
    CardReplaceSerializer,
    DeviceConfigSerializer,
    HeartbeatSerializer,
    RFIDCardAssignmentSerializer,
    RFIDCardSerializer,
    RFIDCardUpdateSerializer,
    RFIDDeviceSerializer,
    RFIDDeviceWithKeySerializer,
    RFIDEventSerializer,
    ScanResponseSerializer,
    ScanSerializer,
)

ACTIVE_ASSIGNMENTS = "assignments__developer"


class RFIDCardViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Cards are never deleted: retire them instead, so scan history stays intact."""

    queryset = RFIDCard.objects.prefetch_related(ACTIVE_ASSIGNMENTS)
    serializer_class = RFIDCardSerializer
    permission_classes = [HasPermissions]
    required_permissions = {
        "list": ["rfid.view"],
        "retrieve": ["rfid.view"],
        "create": ["rfid.assign"],
        "partial_update": ["rfid.assign"],
        "assign": ["rfid.assign"],
        "unassign": ["rfid.assign"],
        "replace": ["rfid.assign"],
        "retire": ["rfid.assign"],
        "block": ["rfid.block"],
        "unblock": ["rfid.block"],
    }
    http_method_names = ["get", "post", "patch", "head", "options"]
    filterset_class = RFIDCardFilter
    search_fields = ["uid", "label", "assignments__developer__full_name"]
    ordering_fields = ["uid", "label", "status", "created_at"]

    def get_serializer_class(self):
        return RFIDCardUpdateSerializer if self.action == "partial_update" else RFIDCardSerializer

    def _card_response(self, card, code=status.HTTP_200_OK):
        card = self.get_queryset().get(pk=card.pk)
        return Response(RFIDCardSerializer(card).data, status=code)

    def perform_create(self, serializer):
        serializer.instance = services.register_card(
            actor=self.request.user, **serializer.validated_data
        )

    def partial_update(self, request, *args, **kwargs):
        card = self.get_object()
        serializer = RFIDCardUpdateSerializer(card, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return self._card_response(card)

    @extend_schema(request=CardAssignSerializer, responses=RFIDCardSerializer)
    @action(detail=True, methods=["post"])
    def assign(self, request, pk=None):
        serializer = CardAssignSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        card = self.get_object()
        services.assign_card(
            actor=request.user, card=card, developer=serializer.validated_data["developer"]
        )
        return self._card_response(card)

    @extend_schema(request=None, responses=RFIDCardSerializer)
    @action(detail=True, methods=["post"])
    def unassign(self, request, pk=None):
        card = self.get_object()
        services.unassign_card(actor=request.user, card=card)
        return self._card_response(card)

    @extend_schema(request=CardReplaceSerializer, responses=RFIDCardSerializer)
    @action(detail=True, methods=["post"])
    def replace(self, request, pk=None):
        """Give the holder of this card a new card; this card is retired. Returns the new card."""
        serializer = CardReplaceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        old_card = self.get_object()
        with transaction.atomic():
            new_card = RFIDCard.objects.filter(uid=data["new_card_uid"]).first()
            if new_card is None:
                new_card = services.register_card(
                    actor=request.user,
                    uid=data["new_card_uid"],
                    label=data.get("new_card_label", ""),
                )
            services.replace_card(
                actor=request.user, old_card=old_card, new_card=new_card, reason=data["reason"]
            )
        return self._card_response(new_card)

    def _transition(self, request, transition):
        serializer = CardReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        card = services.change_card_status(
            actor=request.user,
            card=self.get_object(),
            transition=transition,
            reason=serializer.validated_data["reason"],
        )
        return self._card_response(card)

    @extend_schema(request=CardReasonSerializer, responses=RFIDCardSerializer)
    @action(detail=True, methods=["post"])
    def block(self, request, pk=None):
        """Reject all scans of this card; it stays assigned (e.g. lost, pending replacement)."""
        return self._transition(request, "block")

    @extend_schema(request=CardReasonSerializer, responses=RFIDCardSerializer)
    @action(detail=True, methods=["post"])
    def unblock(self, request, pk=None):
        return self._transition(request, "unblock")

    @extend_schema(request=CardReasonSerializer, responses=RFIDCardSerializer)
    @action(detail=True, methods=["post"])
    def retire(self, request, pk=None):
        """Take an unassigned card out of circulation permanently (broken, destroyed)."""
        return self._transition(request, "retire")


class RFIDCardAssignmentViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = RFIDCardAssignment.objects.select_related("card", "developer")
    serializer_class = RFIDCardAssignmentSerializer
    permission_classes = [HasPermissions]
    required_permissions = {"list": ["rfid.view"], "retrieve": ["rfid.view"]}
    filterset_class = RFIDCardAssignmentFilter
    ordering_fields = ["assigned_at", "unassigned_at"]


class RFIDDeviceViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Deactivate a reader with `PATCH {"is_active": false}`; its key stops working."""

    queryset = RFIDDevice.objects.select_related("service_position__seller", "building")
    serializer_class = RFIDDeviceSerializer
    permission_classes = [HasPermissions]
    required_permissions = {
        "list": ["rfid.view"],
        "retrieve": ["rfid.view"],
        "create": ["rfid.device.manage"],
        "partial_update": ["rfid.device.manage"],
        "rotate_key": ["rfid.device.manage"],
    }
    http_method_names = ["get", "post", "patch", "head", "options"]
    filterset_class = RFIDDeviceFilter
    search_fields = ["code", "name", "location"]
    ordering_fields = ["code", "last_seen_at"]

    @extend_schema(responses={201: RFIDDeviceWithKeySerializer})
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        device, key = services.register_device(actor=request.user, **serializer.validated_data)
        data = RFIDDeviceWithKeySerializer(device, context={"api_key": key}).data
        return Response(data, status=status.HTTP_201_CREATED)

    def perform_update(self, serializer):
        serializer.instance = services.update_device(
            actor=self.request.user, device=serializer.instance, **serializer.validated_data
        )

    @extend_schema(request=None, responses=RFIDDeviceWithKeySerializer)
    @action(detail=True, methods=["post"], url_path="rotate-key")
    def rotate_key(self, request, pk=None):
        device = self.get_object()
        key = services.rotate_device_key(actor=request.user, device=device)
        device.refresh_from_db()
        return Response(RFIDDeviceWithKeySerializer(device, context={"api_key": key}).data)


class RFIDEventViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """GET: scan history for staff. POST: readers submit scans (`Authorization: Device <key>`)."""

    queryset = RFIDEvent.objects.select_related("device", "developer")
    serializer_class = RFIDEventSerializer
    authentication_classes = [
        *api_settings.DEFAULT_AUTHENTICATION_CLASSES,
        DeviceAuthentication,
        DeviceSNAuthentication,
        DeviceIPAuthentication,
    ]
    required_permissions = {"list": ["rfid.view"], "retrieve": ["rfid.view"]}
    filterset_class = RFIDEventFilter
    search_fields = ["uid", "developer__full_name"]
    ordering_fields = ["event_time", "received_at"]

    def get_permissions(self):
        return [IsRFIDDevice()] if self.action in ("create", "batch") else [HasPermissions()]

    @extend_schema(
        request=ScanSerializer,
        responses={201: ScanResponseSerializer, 200: ScanResponseSerializer},
        description="Returns 201 for a new scan, 200 when `client_event_id` was already seen.",
    )
    def create(self, request, *args, **kwargs):
        device = request.auth
        serializer = ScanSerializer(data=request.data, context={"device": device})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        event, created = services.record_scan(
            device=device,
            uid=data["uid"],
            event_time=data.get("event_time"),
            client_event_id=data["client_event_id"],
            direction=data["direction"],
        )
        return Response(
            ScanResponseSerializer(event).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @extend_schema(
        request=BatchScanSerializer,
        responses=BatchResultSerializer(many=True),
        description="ATTENDANCE devices upload scans buffered while offline (oldest first). "
        "Every item needs a client_event_id, so re-sending a batch is safe.",
    )
    @action(detail=False, methods=["post"])
    def batch(self, request):
        device = request.auth
        if device.purpose != DevicePurpose.ATTENDANCE:
            raise ValidationError(
                {"events": ["Only attendance readers may upload buffered scans."]}
            )
        serializer = BatchScanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        results = services.record_batch(device=device, events=serializer.validated_data["events"])
        data = [
            {
                "client_event_id": e.client_event_id,
                "id": e.pk,
                "result": e.result,
                "created": created,
            }
            for e, created in results
        ]
        return Response(BatchResultSerializer(data, many=True).data)


class DeviceHeartbeatView(APIView):
    """Devices call this every RFID_HEARTBEAT_SECONDS with `Authorization: Device <key>`."""

    authentication_classes = [DeviceAuthentication, DeviceSNAuthentication, DeviceIPAuthentication]
    permission_classes = [IsRFIDDevice]

    @extend_schema(request=HeartbeatSerializer, responses=DeviceConfigSerializer)
    def post(self, request):
        serializer = HeartbeatSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ctx = get_request_context()
        device = services.heartbeat(
            device=request.auth,
            ip_address=ctx.ip_address if ctx else None,
            app_version=serializer.validated_data.get("app_version", ""),
        )
        return Response(DeviceConfigSerializer(device).data)


class BuildingViewSet(viewsets.ModelViewSet):
    queryset = Building.objects.all()
    serializer_class = BuildingSerializer
    permission_classes = [HasPermissions]
    required_permissions = {
        "list": ["rfid.view"],
        "retrieve": ["rfid.view"],
        "create": ["rfid.device.manage"],
        "partial_update": ["rfid.device.manage"],
        "destroy": ["rfid.device.manage"],
    }
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    pagination_class = None
    filter_backends = []
