from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers

from apps.developers.models import Developer
from apps.developers.serializers import DeveloperSummarySerializer

from .models import AttendanceRecord, DailyAttendance


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

    def validate_event_time(self, value):
        if value > timezone.now() + timedelta(minutes=5):
            raise serializers.ValidationError("Cannot add attendance in the future.")
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
