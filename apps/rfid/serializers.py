from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework import serializers

from apps.developers.models import Developer
from apps.developers.serializers import DeveloperSummarySerializer

from .models import (
    DevicePurpose,
    RFIDCard,
    RFIDCardAssignment,
    RFIDDevice,
    RFIDEvent,
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
            raise serializers.ValidationError("A card with this UID is already registered.")
        return value


class RFIDCardUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = RFIDCard
        fields = ["label", "notes"]


class CardAssignSerializer(serializers.Serializer):
    developer = serializers.PrimaryKeyRelatedField(queryset=Developer.objects.all())


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

    class Meta:
        model = RFIDDevice
        fields = [
            "id",
            "code",
            "name",
            "location",
            "purpose",
            "service_position",
            "direction",
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
        position = attrs.get("service_position", getattr(self.instance, "service_position", None))
        if purpose == DevicePurpose.TILL and position is None:
            raise serializers.ValidationError(
                {"service_position": ["TILL devices must belong to a service position."]}
            )
        if purpose != DevicePurpose.TILL and position is not None:
            raise serializers.ValidationError(
                {"service_position": ["Only TILL devices belong to a service position."]}
            )
        return attrs


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
    developer = DeveloperSummarySerializer(read_only=True)

    class Meta:
        model = RFIDEvent
        fields = [
            "id",
            "device",
            "device_code",
            "client_event_id",
            "uid",
            "card",
            "developer",
            "event_time",
            "received_at",
            "result",
        ]
        read_only_fields = fields


class ScanSerializer(serializers.Serializer):
    """Payload a reader sends."""

    uid = UIDField()
    device_id = serializers.CharField(
        required=False, help_text="Optional; must match the authenticated reader's code."
    )
    event_time = serializers.DateTimeField(required=False)
    client_event_id = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default=""
    )

    def validate_device_id(self, value):
        if value != self.context["device"].code:
            raise serializers.ValidationError("Does not match the authenticated device.")
        return value

    def validate_event_time(self, value):
        limit = timezone.now() + timedelta(seconds=settings.RFID_MAX_FUTURE_SKEW_SECONDS)
        if value > limit:
            raise serializers.ValidationError(
                "Event time is in the future; check the reader clock."
            )
        return value


DISPLAY_MESSAGES = {
    "UNKNOWN_CARD": "Unknown card",
    "UNASSIGNED_CARD": "Card not assigned",
    "BLOCKED_CARD": "Card blocked",
    "RETIRED_CARD": "Card no longer valid",
    "INACTIVE_DEVELOPER": "Not active - contact your manager",
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
        if obj.result in ("ACCEPTED", "DUPLICATE") and obj.developer is not None:
            if obj.device.purpose == DevicePurpose.TILL:
                if self.get_purchase(obj) is None:
                    return "No open purchase at this counter"
                return f"{obj.developer.full_name} - enter PIN"
            return f"Welcome, {obj.developer.full_name}"
        return DISPLAY_MESSAGES.get(obj.result, obj.result)

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


class BatchScanItemSerializer(serializers.Serializer):
    uid = UIDField()
    event_time = serializers.DateTimeField()
    client_event_id = serializers.CharField(max_length=64)

    def validate_event_time(self, value):
        limit = timezone.now() + timedelta(seconds=settings.RFID_MAX_FUTURE_SKEW_SECONDS)
        if value > limit:
            raise serializers.ValidationError(
                "Event time is in the future; check the reader clock."
            )
        return value


class BatchScanSerializer(serializers.Serializer):
    events = BatchScanItemSerializer(many=True, allow_empty=False)

    def validate_events(self, value):
        if len(value) > settings.RFID_BATCH_MAX_EVENTS:
            raise serializers.ValidationError(
                f"At most {settings.RFID_BATCH_MAX_EVENTS} events per batch."
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
    service_position_name = serializers.CharField(
        source="service_position.name", read_only=True, default=None
    )
    seller_name = serializers.CharField(
        source="service_position.seller.name", read_only=True, default=None
    )

    class Meta:
        model = RFIDDevice
        fields = [
            "code",
            "name",
            "location",
            "purpose",
            "direction",
            "service_position",
            "service_position_name",
            "seller_name",
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
