from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import translation
from django.utils.translation import gettext_lazy as _
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.views import APIView

from apps.developers.services import update_developer
from apps.finance.services import give_pin, open_account, validate_pin_format
from common.context import get_request_context
from common.middleware import client_ip
from common.permissions import HasPermissions

from . import services
from .authentication import (
    DeviceAuthentication,
    DeviceIPAuthentication,
)
from .filters import (
    RFIDCardAssignmentFilter,
    RFIDCardFilter,
    RFIDDeviceFilter,
    RFIDEventFilter,
    TCPFrameLogFilter,
)
from .models import (
    Building,
    DevicePurpose,
    RFIDCard,
    RFIDCardAssignment,
    RFIDDevice,
    RFIDEvent,
    TCPFrameLog,
)
from .permissions import IsRFIDDevice
from .scope import BuildingScopedMixin, building_scope, ensure_in_scope
from .serializers import (
    AssignSellerSerializer,
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
    TCPFrameLogSerializer,
)

ACTIVE_ASSIGNMENTS = "assignments__developer"


class RFIDCardViewSet(
    BuildingScopedMixin,
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

    def filter_by_buildings(self, qs, scope):
        """Building managers: cards of their developers, plus unassigned cards (stock)."""
        active = RFIDCardAssignment.objects.filter(unassigned_at__isnull=True)
        return qs.filter(
            Q(pk__in=active.filter(developer__building__in=scope).values("card"))
            | ~Q(pk__in=active.values("card"))
        )

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
        developer = serializer.validated_data["developer"]
        ensure_in_scope(request.user, "rfid.assign", developer.building_id, field="developer")
        building = serializer.validated_data.get("building")
        if building is not None:
            ensure_in_scope(request.user, "rfid.assign", building.pk)
        validate_pin_format(serializer.validated_data["pin"])
        with transaction.atomic():
            if building is not None and building.pk != developer.building_id:
                update_developer(actor=request.user, developer=developer, building=building)
            services.assign_card(actor=request.user, card=card, developer=developer)
            give_pin(
                actor=request.user,
                account=open_account(developer),
                pin=serializer.validated_data["pin"],
                action="finance.pin_set_at_card_assignment",
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


class RFIDCardAssignmentViewSet(BuildingScopedMixin, viewsets.ReadOnlyModelViewSet):
    queryset = RFIDCardAssignment.objects.select_related("card", "developer")
    serializer_class = RFIDCardAssignmentSerializer
    permission_classes = [HasPermissions]
    required_permissions = {"list": ["rfid.view"], "retrieve": ["rfid.view"]}
    building_lookup = "developer__building"
    filterset_class = RFIDCardAssignmentFilter
    ordering_fields = ["assigned_at", "unassigned_at"]


class RFIDDeviceViewSet(
    BuildingScopedMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Deactivate a reader with `PATCH {"is_active": false}`; its key stops working."""

    queryset = RFIDDevice.objects.select_related("building", "seller")
    serializer_class = RFIDDeviceSerializer
    permission_classes = [HasPermissions]
    required_permissions = {
        "list": ["rfid.view"],
        "retrieve": ["rfid.view"],
        "create": ["rfid.device.manage"],
        "partial_update": ["rfid.device.manage"],
        "rotate_key": ["rfid.device.manage"],
        "assign_seller": ["rfid.device.manage"],
    }
    http_method_names = ["get", "post", "patch", "head", "options"]
    filterset_class = RFIDDeviceFilter
    search_fields = ["code", "name", "location"]
    ordering_fields = ["code", "last_seen_at"]

    @extend_schema(responses={201: RFIDDeviceWithKeySerializer})
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        building = serializer.validated_data.get("building")
        ensure_in_scope(request.user, "rfid.device.manage", building and building.pk)
        device, key = services.register_device(actor=request.user, **serializer.validated_data)
        data = RFIDDeviceWithKeySerializer(device, context={"api_key": key}).data
        return Response(data, status=status.HTTP_201_CREATED)

    def perform_update(self, serializer):
        if "building" in serializer.validated_data:
            building = serializer.validated_data["building"]
            ensure_in_scope(self.request.user, "rfid.device.manage", building and building.pk)
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

    @extend_schema(request=AssignSellerSerializer, responses=RFIDDeviceSerializer)
    @action(detail=True, methods=["post"], url_path="assign-seller")
    def assign_seller(self, request, pk=None):
        """Assign this till reader to a seller (`{"seller": id}`), or unassign it
        (`{"seller": null}`). Readers are registered without a seller."""
        body = AssignSellerSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        device = services.assign_reader(
            actor=request.user, device=self.get_object(), seller=body.validated_data["seller"]
        )
        return Response(RFIDDeviceSerializer(device).data)


class DeviceLanguageMixin:
    """Reader requests get their texts (display messages, errors) in DEVICE_LANGUAGE:
    readers can't choose a language themselves."""

    device_actions: tuple[str, ...] | None = None  # None: every request comes from a device

    def initial(self, request, *args, **kwargs):
        if self.device_actions is None or getattr(self, "action", None) in self.device_actions:
            translation.activate(settings.DEVICE_LANGUAGE)
            request.LANGUAGE_CODE = settings.DEVICE_LANGUAGE
        super().initial(request, *args, **kwargs)


class RFIDEventViewSet(
    DeviceLanguageMixin,
    BuildingScopedMixin,
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
        DeviceIPAuthentication,
    ]
    required_permissions = {"list": ["rfid.view"], "retrieve": ["rfid.view"]}
    device_actions = ("create", "batch")
    building_lookup = "device__building"
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
            source_ip=client_ip(request),
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
                {"events": [_("Only attendance readers may upload buffered scans.")]}
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


NEW_CARD_WINDOW = timedelta(seconds=10)


class CardReadPermission(BasePermission):
    """Card assign staff (`rfid.assign`) and the deposit desk (`finance.deposit`)."""

    def has_permission(self, request, view) -> bool:
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and (user.has_rbac_perm("rfid.assign") or user.has_rbac_perm("finance.deposit"))
        )


class CardReadView(APIView):
    """The card assign, new card and deposit pages poll this while a card is tapped on a
    card assign reader.

    Returns the active card assign readers, `cursor` (the newest tap id of `device`) and,
    when `after` is given, `read`: the newest tap on `device` after that id (or null).
    A newly tapped card is already registered, so `read.card` can be assigned directly;
    `read.card.developer` is the holder (for the deposit desk).
    """

    permission_classes = [CardReadPermission]

    @extend_schema(
        parameters=[
            OpenApiParameter("device", int, description="Card assign reader id."),
            OpenApiParameter("after", int, description="Only taps newer than this tap id."),
        ],
        responses=OpenApiTypes.OBJECT,
    )
    def get(self, request):
        readers = RFIDDevice.objects.filter(purpose=DevicePurpose.ENROLL, is_active=True)
        body = {
            "devices": [
                {"id": d.pk, "code": d.code, "name": d.name, "is_online": d.is_online}
                for d in readers
            ],
            "cursor": None,
            "read": None,
        }
        device = readers.filter(pk=_int_param(request, "device")).first()
        if device is None:
            return Response(body)
        taps = RFIDEvent.objects.filter(device=device).order_by("-pk")
        body["cursor"] = taps.values_list("pk", flat=True).first()
        after = _int_param(request, "after")
        if after is not None:
            tap = taps.filter(pk__gt=after).first()
            if tap is not None:
                body["read"] = self._read(request, tap)
        return Response(body)

    def _read(self, request, tap: RFIDEvent) -> dict:
        card = RFIDCard.objects.filter(uid=tap.uid).first()
        read = {"event": tap.pk, "uid": tap.uid, "event_time": tap.event_time, "card": None}
        if card is None:
            return read
        holder = (
            RFIDCardAssignment.objects.filter(card=card, unassigned_at__isnull=True)
            .select_related("developer")
            .first()
        )
        scope = building_scope(request.user, "rfid.assign")
        visible = holder is not None and (scope is None or holder.developer.building_id in scope)
        read["card"] = {
            "id": card.pk,
            "status": card.status,
            "label": card.label,
            "notes": card.notes,
            # Registered by this tap (see services._register_tapped_card): created just now
            # and never assigned.
            "new": abs(card.created_at - tap.received_at) <= NEW_CARD_WINDOW
            and not RFIDCardAssignment.objects.filter(card=card).exists(),
            "assigned": holder is not None,
            # Holders outside a building manager's buildings stay anonymous.
            "holder": holder.developer.full_name if visible else None,
            "developer": {
                "id": holder.developer.pk,
                "full_name": holder.developer.full_name,
                "employee_number": holder.developer.employee_number,
                "department": holder.developer.department,
                "status": holder.developer.status,
            }
            if visible
            else None,
        }
        return read


def _int_param(request, name: str) -> int | None:
    try:
        return int(request.query_params[name])
    except (KeyError, ValueError):
        return None


class DeviceHeartbeatView(DeviceLanguageMixin, APIView):
    """Devices call this every RFID_HEARTBEAT_SECONDS with `Authorization: Device <key>`."""

    authentication_classes = [DeviceAuthentication, DeviceIPAuthentication]
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


class BuildingViewSet(BuildingScopedMixin, viewsets.ModelViewSet):
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
    building_lookup = "pk"

    def _scoped(self) -> bool:
        return building_scope(self.request.user, "rfid.device.manage") is not None

    def perform_create(self, serializer):
        if self._scoped():
            raise PermissionDenied(_("Building managers can't create buildings."))
        serializer.save()

    def perform_update(self, serializer):
        if self._scoped():
            for field in ("managers", "owners"):
                if field in serializer.validated_data:
                    raise ValidationError(
                        {field: [_("Building managers can't change who manages a building.")]}
                    )
        serializer.save()

    def perform_destroy(self, instance):
        if self._scoped():
            raise PermissionDenied(_("Building managers can't delete buildings."))
        instance.delete()


class TCPFrameLogViewSet(viewsets.ReadOnlyModelViewSet):
    """Every packet the TCP listener received (`request`) and what it sent back (`response`),
    newest first, kept for RFID_TCP_LOG_DAYS. Admins only (`system.tcp_log`)."""

    queryset = TCPFrameLog.objects.select_related("device")
    serializer_class = TCPFrameLogSerializer
    permission_classes = [HasPermissions]
    required_permissions = {"list": ["system.tcp_log"], "retrieve": ["system.tcp_log"]}
    filterset_class = TCPFrameLogFilter
    ordering_fields = ["received_at", "duration_ms"]
