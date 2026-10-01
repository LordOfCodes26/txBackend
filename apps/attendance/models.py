from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.developers.models import Developer
from apps.rfid.models import Building, RFIDDevice, RFIDEvent
from common.models import TimeStampedModel


class EventType(models.TextChoices):
    IN = "IN", "In"
    OUT = "OUT", "Out"
    SCAN = "SCAN", "Scan (direction not classified)"


class RecordSource(models.TextChoices):
    RFID = "RFID", "RFID scan"
    MANUAL = "MANUAL", "Manual correction"


class AttendanceRecord(TimeStampedModel):
    """A moment a developer was seen. Derived from accepted RFID scans or added manually.

    `event_type` is recalculated for the whole day by the configured rule whenever the
    day changes, so it may change after the fact; the time and source never do. Wrong
    records are voided (kept, with a reason), never deleted.
    """

    developer = models.ForeignKey(
        Developer, on_delete=models.PROTECT, related_name="attendance_records"
    )
    work_date = models.DateField(help_text="Company-local working day this record counts for.")
    event_time = models.DateTimeField()
    event_type = models.CharField(max_length=4, choices=EventType.choices, default=EventType.SCAN)
    direction = models.CharField(
        max_length=3,
        choices=[("IN", "In"), ("OUT", "Out")],
        blank=True,
        help_text="In/out as reported by the door, or as entered on a manual correction.",
    )
    source = models.CharField(max_length=6, choices=RecordSource.choices)
    rfid_event = models.OneToOneField(
        RFIDEvent,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="attendance_record",
    )
    device = models.ForeignKey(
        RFIDDevice, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    is_void = models.BooleanField(default=False)
    void_reason = models.CharField(max_length=255, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    voided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["event_time", "id"]
        indexes = [
            models.Index(fields=["developer", "work_date"]),
            # Occupancy looks up each developer's latest record.
            models.Index(fields=["developer", "-event_time", "-id"], name="attendance_latest_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(source=RecordSource.MANUAL) | Q(rfid_event__isnull=False),
                name="attendance_rfid_record_has_event",
            ),
            models.CheckConstraint(
                condition=Q(source=RecordSource.RFID) | ~Q(note=""),
                name="attendance_manual_record_has_note",
            ),
            models.CheckConstraint(
                condition=Q(is_void=False) | ~Q(void_reason=""),
                name="attendance_void_has_reason",
            ),
        ]

    def __str__(self):
        return f"{self.developer} {self.event_time:%Y-%m-%d %H:%M} {self.event_type}"


class DayStatus(models.TextChoices):
    PRESENT = "PRESENT", "Present"
    INCOMPLETE = "INCOMPLETE", "Incomplete (missing or unpaired scan)"


class DailyAttendance(models.Model):
    """Per-developer, per-day summary, rebuilt from the day's non-void records.

    No row means no records that day. Absence rules (working days, holidays, leave)
    are not modelled yet.
    """

    developer = models.ForeignKey(
        Developer, on_delete=models.PROTECT, related_name="daily_attendance"
    )
    work_date = models.DateField()
    first_seen = models.DateTimeField()
    last_seen = models.DateTimeField(null=True, blank=True)
    record_count = models.PositiveIntegerField()
    worked_seconds = models.PositiveIntegerField()
    status = models.CharField(max_length=10, choices=DayStatus.choices)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-work_date", "developer_id"]
        verbose_name_plural = "daily attendance"
        constraints = [
            models.UniqueConstraint(
                fields=["developer", "work_date"], name="attendance_one_summary_per_day"
            ),
        ]
        indexes = [models.Index(fields=["work_date", "status"])]

    def __str__(self):
        return f"{self.developer} {self.work_date} {self.status}"


class DeveloperPresence(models.Model):
    """Where each developer is right now, derived from their latest attendance record.

    Refreshed (for one developer) whenever that developer's records change, so occupancy
    counts are a cheap query however much history accumulates. Not the source of truth:
    `attendance.occupancy.refresh_presence` rebuilds it from the records at any time.
    """

    developer = models.OneToOneField(
        Developer, on_delete=models.CASCADE, primary_key=True, related_name="presence"
    )
    is_inside = models.BooleanField(default=False)
    building = models.ForeignKey(
        Building, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    since = models.DateTimeField(null=True, blank=True, help_text="Time of the latest record.")
    record = models.ForeignKey(
        AttendanceRecord, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["is_inside", "building"])]

    def __str__(self):
        return f"{self.developer} {'inside' if self.is_inside else 'outside'}"
