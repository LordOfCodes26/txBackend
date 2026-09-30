from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework import serializers

from apps.developers.models import Developer
from apps.developers.serializers import DeveloperSummarySerializer

from .models import (
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
    class Meta:
        model = RFIDDevice
        fields = [
            "id",
            "code",
            "name",
            "location",
            "purpose",
            "direction",
            "is_active",
            "api_key_prefix",
            "last_seen_at",
            "created_at",
        ]
        read_only_fields = ["id", "api_key_prefix", "last_seen_at", "created_at"]


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


class ScanResponseSerializer(serializers.ModelSerializer):
    accepted = serializers.SerializerMethodField()
    developer = DeveloperSummarySerializer(read_only=True)

    class Meta:
        model = RFIDEvent
        fields = ["id", "result", "accepted", "developer", "event_time", "client_event_id"]
        read_only_fields = fields

    def get_accepted(self, obj) -> bool:
        return obj.result == "ACCEPTED"
