from datetime import date, datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.audit.services import record_audit
from apps.developers.models import Developer
from apps.rfid.models import DevicePurpose, RFIDEvent, ScanResult

from .exceptions import RecordAlreadyVoid
from .models import AttendanceRecord, DailyAttendance, DayStatus, RecordSource
from .rules import RULES, worked_time


def work_date_for(moment: datetime) -> date:
    """Company-local working day. Scans before ATTENDANCE_DAY_START_HOUR count for the
    previous day, so a night shift ending at 02:00 stays on the day it started."""
    local = timezone.localtime(moment)
    return (local - timedelta(hours=settings.ATTENDANCE_DAY_START_HOUR)).date()


def record_from_scan(event: RFIDEvent) -> AttendanceRecord | None:
    """Turn an accepted scan into attendance. Runs inside the scan's transaction."""
    if event.result != ScanResult.ACCEPTED or event.device.purpose != DevicePurpose.ATTENDANCE:
        return None
    record = AttendanceRecord.objects.create(
        developer_id=event.developer_id,
        work_date=work_date_for(event.event_time),
        event_time=event.event_time,
        source=RecordSource.RFID,
        rfid_event=event,
        device_id=event.device_id,
    )
    recompute_day(event.developer_id, record.work_date)
    return record


@transaction.atomic
def add_manual_record(*, actor, developer: Developer, event_time: datetime, note: str):
    record = AttendanceRecord.objects.create(
        developer=developer,
        work_date=work_date_for(event_time),
        event_time=event_time,
        source=RecordSource.MANUAL,
        note=note,
        created_by=actor,
    )
    recompute_day(developer.pk, record.work_date)
    record_audit(
        "attendance.record_added",
        actor=actor,
        entity=record,
        new_values={"developer": developer.pk, "event_time": event_time, "note": note},
    )
    record.refresh_from_db()
    return record


@transaction.atomic
def void_record(*, actor, record: AttendanceRecord, reason: str) -> AttendanceRecord:
    record = AttendanceRecord.objects.select_for_update().get(pk=record.pk)
    if record.is_void:
        raise RecordAlreadyVoid()
    record.is_void = True
    record.void_reason = reason
    record.voided_by = actor
    record.voided_at = timezone.now()
    record.save(update_fields=["is_void", "void_reason", "voided_by", "voided_at", "updated_at"])
    recompute_day(record.developer_id, record.work_date)
    record_audit(
        "attendance.record_voided",
        actor=actor,
        entity=record,
        old_values={"is_void": False},
        new_values={"is_void": True, "reason": reason},
    )
    return record


def recompute_day(developer_id: int, work_date: date) -> DailyAttendance | None:
    """Relabel the day's records and rebuild its summary. Must run inside a transaction."""
    # Lock the developer so a scan and a manual correction cannot interleave here.
    Developer.all_objects.select_for_update().filter(pk=developer_id).exists()

    records = list(
        AttendanceRecord.objects.select_related("device", "rfid_event")
        .filter(developer_id=developer_id, work_date=work_date, is_void=False)
        .order_by("event_time", "id")
    )
    if not records:
        DailyAttendance.objects.filter(developer_id=developer_id, work_date=work_date).delete()
        return None

    types = RULES[settings.ATTENDANCE_DIRECTION_RULE](records)
    changed = []
    for record, kind in zip(records, types, strict=True):
        if record.event_type != kind:
            record.event_type = kind
            changed.append(record)
    AttendanceRecord.objects.bulk_update(changed, ["event_type"])

    worked, complete = worked_time([r.event_time for r in records], types)
    summary, _ = DailyAttendance.objects.update_or_create(
        developer_id=developer_id,
        work_date=work_date,
        defaults={
            "first_seen": records[0].event_time,
            "last_seen": records[-1].event_time if len(records) > 1 else None,
            "record_count": len(records),
            "worked_seconds": worked,
            "status": DayStatus.PRESENT if complete else DayStatus.INCOMPLETE,
        },
    )
    return summary


def rebuild(*, date_from: date | None = None, date_to: date | None = None) -> dict:
    """Create records for accepted scans that have none, then recompute every affected
    day. Use after changing the rule or the day-start hour."""
    missing = RFIDEvent.objects.filter(
        result=ScanResult.ACCEPTED,
        device__purpose=DevicePurpose.ATTENDANCE,
        attendance_record__isnull=True,
    ).select_related("device")
    created = 0
    for event in missing.iterator():
        day = work_date_for(event.event_time)
        if (date_from and day < date_from) or (date_to and day > date_to):
            continue
        with transaction.atomic():
            AttendanceRecord.objects.create(
                developer_id=event.developer_id,
                work_date=day,
                event_time=event.event_time,
                source=RecordSource.RFID,
                rfid_event=event,
                device_id=event.device_id,
            )
        created += 1

    records = AttendanceRecord.objects.all()
    if date_from:
        records = records.filter(work_date__gte=date_from)
    if date_to:
        records = records.filter(work_date__lte=date_to)
    days = set(records.values_list("developer_id", "work_date").distinct())

    # A changed day-start hour can move records to another work_date.
    moved = 0
    for record in records.iterator():
        day = work_date_for(record.event_time)
        if day != record.work_date:
            days.add((record.developer_id, record.work_date))
            days.add((record.developer_id, day))
            AttendanceRecord.objects.filter(pk=record.pk).update(work_date=day)
            moved += 1

    for developer_id, day in sorted(days):
        with transaction.atomic():
            recompute_day(developer_id, day)
    return {"records_created": created, "records_moved": moved, "days_recomputed": len(days)}
