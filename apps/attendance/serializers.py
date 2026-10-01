from datetime import timedelta

from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from apps.developers.models import Developer
from apps.developers.serializers import DeveloperSummarySerializer

from .models import AttendanceRecord, DailyAttendance, DeveloperPresence


class AttendanceRecordSerializer(serializers.ModelSerializer):
    developer = DeveloperSummarySerializer(read_only=True)
    device_code = serializers.CharField(source="device.code", read_only=True, default=None)

    class Meta:
        model = AttendanceRecord
        fields = [
            "id",
            "developer",
            "work_date",
            "event_time",
            "event_type",
            "direction",
            "source",
            "device_code",
            "rfid_event",
            "note",
            "created_by",
            "is_void",
            "void_reason",
            "voided_by",
            "voided_at",
        ]
        read_only_fields = fields


class ManualRecordSerializer(serializers.Serializer):
    developer = serializers.PrimaryKeyRelatedField(queryset=Developer.objects.all())
    event_time = serializers.DateTimeField()
    note = serializers.CharField(max_length=255, help_text="Why the record is being added.")
    direction = serializers.ChoiceField(
        choices=["IN", "OUT"],
        required=False,
        allow_blank=True,
        default="",
        help_text="IN or OUT, e.g. OUT to mark someone as gone who never scanned out.",
    )

    def validate_event_time(self, value):
        if value > timezone.now() + timedelta(minutes=5):
            raise serializers.ValidationError(_("Cannot add attendance in the future."))
        return value


class VoidSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=255)


class DailyAttendanceSerializer(serializers.ModelSerializer):
    developer = DeveloperSummarySerializer(read_only=True)
    worked_hours = serializers.SerializerMethodField()

    class Meta:
        model = DailyAttendance
        fields = [
            "id",
            "developer",
            "work_date",
            "first_seen",
            "last_seen",
            "record_count",
            "worked_seconds",
            "worked_hours",
            "status",
        ]
        read_only_fields = fields

    def get_worked_hours(self, obj) -> float:
        return round(obj.worked_seconds / 3600, 2)


class OccupancyBuildingSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()
    count = serializers.IntegerField()
    developers = serializers.IntegerField(
        help_text="Active developers whose latest scan was at this building, present or left."
    )


class OccupancySerializer(serializers.Serializer):
    as_of = serializers.DateTimeField()
    total = serializers.IntegerField(help_text="Developers inside any building right now.")
    buildings = OccupancyBuildingSerializer(many=True)
    unknown_building = serializers.IntegerField(
        help_text="Inside, but last IN had no building (e.g. a manual correction)."
    )


class PersonInsideSerializer(serializers.ModelSerializer):
    developer = DeveloperSummarySerializer(read_only=True)
    building = serializers.SerializerMethodField()
    device_code = serializers.CharField(source="record.device.code", read_only=True, default=None)

    class Meta:
        model = DeveloperPresence
        fields = ["developer", "building", "since", "device_code"]
        read_only_fields = fields

    def get_building(self, presence) -> dict | None:
        b = presence.building
        return None if b is None else {"id": b.pk, "code": b.code, "name": b.name}
