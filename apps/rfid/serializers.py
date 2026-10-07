from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from apps.developers.models import Developer
from apps.developers.serializers import DeveloperSummarySerializer
from apps.finance.serializers import NewPinSerializer
from apps.sellers.models import Seller

from .models import (
    Building,
    DevicePurpose,
    RFIDCard,
    RFIDCardAssignment,
    RFIDDevice,
    RFIDEvent,
    ScanDirection,
    TCPFrameLog,
    normalize_uid,
    uid_validator,
)


class UIDField(serializers.CharField):
    def __init__(self, **kwargs):
        kwargs.setdefault("max_length", 64)
        super().__init__(**kwargs)
        self.validators.append(uid_validator)

    def to_internal_value(self, data):
        return normalize_uid(super().to_internal_value(data))


class CurrentAssignmentSerializer(serializers.ModelSerializer):
    developer = DeveloperSummarySerializer(read_only=True)

    class Meta:
        model = RFIDCardAssignment
        fields = ["id", "developer", "assigned_at"]


class RFIDCardSerializer(serializers.ModelSerializer):
    uid = UIDField()
    current_assignment = CurrentAssignmentSerializer(read_only=True, allow_null=True)

    class Meta:
        model = RFIDCard
        fields = [
            "id",
            "uid",
            "label",
            "status",
            "status_reason",
            "notes",
            "current_assignment",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "status", "status_reason", "created_at", "updated_at"]

    def validate_uid(self, value):
        qs = RFIDCard.objects.filter(uid=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(_("A card with this UID is already registered."))
        return value


class RFIDCardUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = RFIDCard
        fields = ["label", "notes"]


class CardAssignSerializer(NewPinSerializer):
    """Assigning a card also sets the developer's purchase PIN: the developer types it
    twice on the assigning staff member's screen."""

    developer = serializers.PrimaryKeyRelatedField(queryset=Developer.objects.all())
    building = serializers.PrimaryKeyRelatedField(
        queryset=Building.objects.all(),
        required=False,
        help_text="Also set the developer's home building. Leave out to keep it unchanged.",
    )


class CardReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")


class CardReplaceSerializer(serializers.Serializer):
    new_card_uid = UIDField(help_text="Registered automatically if it is a new card.")
    new_card_label = serializers.CharField(max_length=50, required=False, allow_blank=True)
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")


class RFIDCardAssignmentSerializer(serializers.ModelSerializer):
    card_uid = serializers.CharField(source="card.uid", read_only=True)
    developer = DeveloperSummarySerializer(read_only=True)

    class Meta:
        model = RFIDCardAssignment
        fields = [
            "id",
            "card",
            "card_uid",
            "developer",
            "assigned_at",
            "assigned_by",
            "unassigned_at",
            "unassigned_by",
            "end_reason",
        ]
        read_only_fields = fields


class RFIDDeviceSerializer(serializers.ModelSerializer):
    online = serializers.BooleanField(source="is_online", read_only=True)
    seller_name = serializers.CharField(source="seller.name", read_only=True, default=None)
    building_name = serializers.CharField(source="building.name", read_only=True, default=None)

    class Meta:
        model = RFIDDevice
        fields = [
            "id",
            "code",
            "name",
            "location",
            "purpose",
            "building",
            "direction",
            "allowed_ip",
            "seller",
            "seller_name",
            "building_name",
            "is_active",
            "online",
            "last_seen_at",
            "last_ip",
            "app_version",
            "api_key_prefix",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "online",
            "last_seen_at",
            "last_ip",
            "app_version",
            "api_key_prefix",
            "created_at",
        ]

    def validate(self, attrs):
        purpose = attrs.get("purpose", getattr(self.instance, "purpose", DevicePurpose.ATTENDANCE))
        building = attrs.get("building", getattr(self.instance, "building", None))
        if purpose != DevicePurpose.ATTENDANCE and building is not None:
            raise serializers.ValidationError(
                {"building": [_("Only ATTENDANCE devices belong to a building.")]}
            )
        allowed_ip = attrs.get("allowed_ip", getattr(self.instance, "allowed_ip", None))
        if purpose != DevicePurpose.ATTENDANCE and allowed_ip:
            raise serializers.ValidationError(
                {"allowed_ip": [_("Only ATTENDANCE door devices may authenticate by IP.")]}
            )
        seller = attrs.get("seller", getattr(self.instance, "seller", None))
        if purpose != DevicePurpose.TILL and seller is not None:
            raise serializers.ValidationError(
                {"seller": [_("Only till readers belong to a seller.")]}
            )
        code = attrs.get("code", getattr(self.instance, "code", ""))
        self._check_code(code.strip(), purpose, allowed_ip)
        return attrs

    def _check_code(self, code, purpose, allowed_ip):
        """Tills and card assign readers are found by their ID alone, so it is unique.
        Door units may share an ID (several devices of one door), each with its own IP."""
        same = RFIDDevice.objects.filter(code__iexact=code)
        if self.instance is not None:
            same = same.exclude(pk=self.instance.pk)
        if (
            purpose != DevicePurpose.ATTENDANCE
            or same.exclude(purpose=DevicePurpose.ATTENDANCE).exists()
        ):
            if same.exists():
                raise serializers.ValidationError(
                    {"code": [_("Another device already uses this ID.")]}
                )
            return
        if not same.exists():
            return
        if not allowed_ip:
            raise serializers.ValidationError(
                {
                    "allowed_ip": [
                        _("Door units that share an ID need their own fixed IP to tell them apart.")
                    ]
                }
            )
        if same.filter(allowed_ip__isnull=True).exists():
            raise serializers.ValidationError(
                {"code": [_("Another unit with this ID has no fixed IP yet; give it one first.")]}
            )
        if same.filter(allowed_ip=allowed_ip).exists():
            raise serializers.ValidationError(
                {"allowed_ip": [_("Another unit of this door already uses this IP.")]}
            )


class BuildingSerializer(serializers.ModelSerializer):
    managers = serializers.PrimaryKeyRelatedField(
        many=True,
        required=False,
        queryset=get_user_model().objects.filter(is_active=True),
        help_text="User ids of the building's managers (give them the BUILDING_MANAGER role).",
    )
    owners = serializers.PrimaryKeyRelatedField(
        many=True,
        required=False,
        queryset=get_user_model().objects.filter(is_active=True),
        help_text="User ids of the building's owners (give them the BUILDING_OWNER role).",
    )

    class Meta:
        model = Building
        fields = ["id", "code", "name", "managers", "owners", "created_at"]
        read_only_fields = ["id", "created_at"]

    @staticmethod
    def _with_role(users, role: str):
        from apps.accounts.models import UserRole

        missing = [
            u.username
            for u in users
            if not UserRole.objects.filter(user=u, role__code=role).exists()
        ]
        if missing:
            raise serializers.ValidationError(
                _("These users don't have the %(role)s role: %(users)s")
                % {"role": role, "users": ", ".join(missing)}
            )
        return users

    def validate_managers(self, users):
        return self._with_role(users, "BUILDING_MANAGER")

    def validate_owners(self, users):
        return self._with_role(users, "BUILDING_OWNER")


class RFIDDeviceWithKeySerializer(RFIDDeviceSerializer):
    api_key = serializers.SerializerMethodField(
        help_text="Shown only once. Configure the reader with it now."
    )

    class Meta(RFIDDeviceSerializer.Meta):
        fields = [*RFIDDeviceSerializer.Meta.fields, "api_key"]

    def get_api_key(self, obj) -> str:
        return self.context["api_key"]


class RFIDEventSerializer(serializers.ModelSerializer):
    device_code = serializers.CharField(source="device.code", read_only=True)
    device_name = serializers.SerializerMethodField(help_text="The reader's name, else its code.")
    developer = DeveloperSummarySerializer(read_only=True)

    class Meta:
        model = RFIDEvent
        fields = [
            "id",
            "device",
            "device_code",
            "device_name",
            "client_event_id",
            "uid",
            "card",
            "developer",
            "event_time",
            "received_at",
            "direction",
            "result",
        ]
        read_only_fields = fields

    def get_device_name(self, event) -> str:
        return event.device.name or event.device.code


# Field names used by the door devices: {"ID": "Door1", "Type": "in", "UID": "..."}.
# Keys are matched case-insensitively and mapped onto our names.
_DEVICE_FIELD_ALIASES = {"id": "device_id", "direction": "type"}

PAY = "PAY"
MASTER = "MASTER"
# The devices' own words for the scan type: TYPE:Input / TYPE:Output at doors.
_TYPE_ALIASES = {"INPUT": "IN", "OUTPUT": "OUT"}


class DirectionField(serializers.ChoiceField):
    def __init__(self, **kwargs):
        super().__init__(choices=ScanDirection.choices, **kwargs)

    def to_internal_value(self, data):
        value = str(data).strip().upper()
        return super().to_internal_value(_TYPE_ALIASES.get(value, value))


class ScanTypeField(serializers.ChoiceField):
    """`in`/`input` and `out`/`output` from building doors, `pay` from till readers,
    `master` from card assign readers (case-insensitive)."""

    def __init__(self, **kwargs):
        super().__init__(choices=[*ScanDirection.values, PAY, MASTER], **kwargs)

    def to_internal_value(self, data):
        value = str(data).strip().upper()
        return super().to_internal_value(_TYPE_ALIASES.get(value, value))


class ScanSerializer(serializers.Serializer):
    """Payload a reader sends. Accepts both our field names and the door devices' format
    `{"ID": "Door1", "Type": "in", "UID": "04A2B3C4"}`."""

    uid = UIDField()
    device_id = serializers.CharField(
        required=False, help_text="Device code (the doors' `ID`); must match the device's key."
    )
    type = ScanTypeField(
        required=False,
        allow_blank=True,
        default="",
        help_text="`in` / `out` from building doors, `pay` from till readers (`Type`/`TYPE`).",
    )
    event_time = serializers.DateTimeField(required=False)
    client_event_id = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default=""
    )

    def to_internal_value(self, data):
        if hasattr(data, "items"):
            normalized = {}
            for key, value in data.items():
                name = str(key).lower()
                normalized[_DEVICE_FIELD_ALIASES.get(name, name)] = value
            data = normalized
        return super().to_internal_value(data)

    def validate_device_id(self, value):
        device = self.context["device"]
        if value.strip().lower() != device.code.lower():
            raise serializers.ValidationError(_("Does not match the authenticated device."))
        return value

    def validate(self, attrs):
        """The scan type must fit the device: doors send in/out, till readers send pay,
        card assign readers send master. A mismatch usually means a device is configured
        with another device's ID or key."""
        kind = attrs.pop("type", "")
        purpose = self.context["device"].purpose
        if purpose == DevicePurpose.TILL and kind not in ("", PAY):
            raise serializers.ValidationError({"type": [_("Till readers send `pay`.")]})
        if purpose == DevicePurpose.ENROLL:
            if kind not in ("", MASTER):
                raise serializers.ValidationError(
                    {"type": [_("Card assign readers send `master`.")]}
                )
            attrs["direction"] = ""  # card assign readers only read the UID
            return attrs
        if kind == MASTER:
            raise serializers.ValidationError(
                {"type": [_("Only card assign readers send `master`.")]}
            )
        if purpose == DevicePurpose.ATTENDANCE and kind == PAY:
            raise serializers.ValidationError({"type": [_("Door devices send `in` or `out`.")]})
        attrs["direction"] = kind if kind in ScanDirection.values else ""
        return attrs

    def validate_event_time(self, value):
        limit = timezone.now() + timedelta(seconds=settings.RFID_MAX_FUTURE_SKEW_SECONDS)
        if value > limit:
            raise serializers.ValidationError(
                _("Event time is in the future; check the reader clock.")
            )
        return value


DISPLAY_MESSAGES = {
    "UNKNOWN_CARD": _("Unknown card"),
    "UNASSIGNED_CARD": _("Card not assigned"),
    "BLOCKED_CARD": _("Card blocked"),
    "RETIRED_CARD": _("Card no longer valid"),
    "INACTIVE_DEVELOPER": _("Not active - contact your manager"),
}


class ScanResponseSerializer(serializers.ModelSerializer):
    accepted = serializers.SerializerMethodField()
    developer = DeveloperSummarySerializer(read_only=True)
    display_message = serializers.SerializerMethodField(
        help_text="Short text for the reader's screen."
    )
    purchase = serializers.SerializerMethodField(
        help_text="TILL devices: the draft purchase this tap was attached to, or null."
    )

    class Meta:
        model = RFIDEvent
        fields = [
            "id",
            "result",
            "accepted",
            "direction",
            "display_message",
            "developer",
            "purchase",
            "event_time",
            "client_event_id",
        ]
        read_only_fields = fields

    def get_accepted(self, obj) -> bool:
        return obj.result in ("ACCEPTED", "DUPLICATE")

    def get_display_message(self, obj) -> str:
        if obj.device.purpose == DevicePurpose.ENROLL:
            return _("Card read: %(uid)s") % {"uid": obj.uid}
        if obj.result in ("ACCEPTED", "DUPLICATE") and obj.developer is not None:
            if obj.device.purpose == DevicePurpose.TILL:
                if self.get_purchase(obj) is None:
                    return _("No purchase is waiting for a card")
                return _("%(name)s - enter PIN") % {"name": obj.developer.full_name}
            if obj.direction == "OUT":
                return _("Goodbye, %(name)s") % {"name": obj.developer.full_name}
            return _("Welcome, %(name)s") % {"name": obj.developer.full_name}
        return str(DISPLAY_MESSAGES.get(obj.result, obj.result))

    def get_purchase(self, obj) -> int | None:
        if obj.device.purpose != DevicePurpose.TILL:
            return None
        if "_purchase" not in self.context:
            from apps.purchases.models import Purchase

            self.context["_purchase"] = (
                Purchase.objects.filter(presented_event=obj).values_list("pk", flat=True).first()
            )
        return self.context["_purchase"]


class HeartbeatSerializer(serializers.Serializer):
    app_version = serializers.CharField(max_length=50, required=False, allow_blank=True)
    ID = serializers.CharField(required=False, help_text="Device code; needed for key-less doors.")


class BatchScanItemSerializer(serializers.Serializer):
    uid = UIDField()
    event_time = serializers.DateTimeField()
    client_event_id = serializers.CharField(max_length=64)
    type = DirectionField(required=False, allow_blank=True, default="")

    def to_internal_value(self, data):
        if hasattr(data, "items"):
            data = {
                _DEVICE_FIELD_ALIASES.get(str(k).lower(), str(k).lower()): v
                for k, v in data.items()
            }
        attrs = super().to_internal_value(data)
        attrs["direction"] = attrs.pop("type", "")
        return attrs

    def validate_event_time(self, value):
        limit = timezone.now() + timedelta(seconds=settings.RFID_MAX_FUTURE_SKEW_SECONDS)
        if value > limit:
            raise serializers.ValidationError(
                _("Event time is in the future; check the reader clock.")
            )
        return value


class BatchScanSerializer(serializers.Serializer):
    events = BatchScanItemSerializer(many=True, allow_empty=False)
    ID = serializers.CharField(
        required=False, help_text="Device code; needed only for IP-authenticated doors."
    )

    def validate_events(self, value):
        if len(value) > settings.RFID_BATCH_MAX_EVENTS:
            raise serializers.ValidationError(
                _("At most %(max)s events per batch.") % {"max": settings.RFID_BATCH_MAX_EVENTS}
            )
        ids = [e["client_event_id"] for e in value]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError("client_event_id values must be unique.")
        return value


class BatchResultSerializer(serializers.Serializer):
    client_event_id = serializers.CharField()
    id = serializers.IntegerField()
    result = serializers.CharField()
    created = serializers.BooleanField(help_text="False when this scan was uploaded before.")


class DeviceConfigSerializer(serializers.ModelSerializer):
    """What a device needs to know about itself and the server (heartbeat response)."""

    server_time = serializers.SerializerMethodField(help_text="Use to correct the device clock.")
    heartbeat_seconds = serializers.SerializerMethodField()
    debounce_seconds = serializers.SerializerMethodField()
    max_future_skew_seconds = serializers.SerializerMethodField()

    class Meta:
        model = RFIDDevice
        fields = [
            "code",
            "name",
            "location",
            "purpose",
            "direction",
            "server_time",
            "heartbeat_seconds",
            "debounce_seconds",
            "max_future_skew_seconds",
        ]
        read_only_fields = fields

    def get_server_time(self, obj) -> str:
        return timezone.now().isoformat()

    def get_heartbeat_seconds(self, obj) -> int:
        return settings.RFID_HEARTBEAT_SECONDS

    def get_debounce_seconds(self, obj) -> int:
        return settings.RFID_DEBOUNCE_SECONDS

    def get_max_future_skew_seconds(self, obj) -> int:
        return settings.RFID_MAX_FUTURE_SKEW_SECONDS


class TCPFrameLogSerializer(serializers.ModelSerializer):
    device_name = serializers.CharField(source="device.name", read_only=True, default=None)
    device_purpose = serializers.CharField(source="device.purpose", read_only=True, default=None)

    class Meta:
        model = TCPFrameLog
        fields = [
            "id",
            "received_at",
            "peer_ip",
            "request",
            "response",
            "device_code",
            "device",
            "device_name",
            "device_purpose",
            "event",
            "outcome",
            "note",
            "duration_ms",
        ]
        read_only_fields = fields


class AssignSellerSerializer(serializers.Serializer):
    seller = serializers.PrimaryKeyRelatedField(
        queryset=Seller.objects.all(), allow_null=True, help_text="null: unassign"
    )
