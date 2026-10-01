from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from apps.accounts.rbac import Roles
from apps.attendance import services
from apps.attendance.models import AttendanceRecord, DailyAttendance
from apps.attendance.rules import worked_time
from apps.audit.models import AuditLog
from apps.developers.models import Developer
from apps.rfid import services as rfid
from apps.rfid.models import DeviceDirection, RFIDCard, RFIDCardAssignment

pytestmark = pytest.mark.django_db

UTC = ZoneInfo("UTC")
RECORDS = "/api/v1/attendance/records/"
DAILY = "/api/v1/attendance/daily/"


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


@pytest.fixture
def developer(db):
    return Developer.objects.create(employee_number="E1", full_name="Ada")


@pytest.fixture
def card(developer):
    card = RFIDCard.objects.create(uid="04AABBCCDD")
    RFIDCardAssignment.objects.create(card=card, developer=developer)
    return card


@pytest.fixture
def reader(db):
    return rfid.register_device(actor=None, code="R1")[0]


def scan(device, card, moment):
    return rfid.record_scan(device=device, uid=card.uid, event_time=moment)[0]


def day_summary(developer, day=21):
    return DailyAttendance.objects.get(developer=developer, work_date=f"2026-09-{day:02d}")


def types(developer):
    return list(
        AttendanceRecord.objects.filter(developer=developer, is_void=False)
        .order_by("event_time")
        .values_list("event_type", flat=True)
    )


# --- Scans become attendance -------------------------------------------------


def test_accepted_scans_create_records_and_summary(reader, card, developer):
    scan(reader, card, at(21, 9))
    scan(reader, card, at(21, 9, 0) + timedelta(seconds=3))  # debounced: no record
    scan(reader, card, at(21, 17, 30))

    assert AttendanceRecord.objects.count() == 2
    summary = day_summary(developer)
    assert (summary.first_seen, summary.last_seen) == (at(21, 9), at(21, 17, 30))
    assert summary.record_count == 2
    assert summary.worked_seconds == 8.5 * 3600
    assert summary.status == "PRESENT"


def test_rejected_scans_create_no_attendance(reader, card):
    card.status = "BLOCKED"
    card.save()
    scan(reader, card, at(21, 9))
    rfid.record_scan(device=reader, uid="0499999999", event_time=at(21, 9))
    assert not AttendanceRecord.objects.exists()


def test_single_scan_day_is_incomplete(reader, card, developer):
    scan(reader, card, at(21, 9))
    summary = day_summary(developer)
    assert summary.status == "INCOMPLETE" and summary.last_seen is None
    assert summary.worked_seconds == 0


def test_out_of_order_buffered_scan_is_placed_correctly(reader, card, developer, settings):
    settings.ATTENDANCE_DIRECTION_RULE = "toggle"
    scan(reader, card, at(21, 17))
    scan(reader, card, at(21, 9))  # arrives late from a reader buffer
    assert types(developer) == ["IN", "OUT"]
    assert day_summary(developer).worked_seconds == 8 * 3600


# --- Rules -------------------------------------------------------------------


def test_none_rule_labels_scans_without_direction(reader, card, developer):
    for hour in (9, 12, 13, 17):
        scan(reader, card, at(21, hour))
    assert types(developer) == ["SCAN"] * 4
    assert day_summary(developer).worked_seconds == 8 * 3600


def test_toggle_rule_pairs_scans(reader, card, developer, settings):
    settings.ATTENDANCE_DIRECTION_RULE = "toggle"
    for hour in (9, 12, 13, 17):
        scan(reader, card, at(21, hour))
    assert types(developer) == ["IN", "OUT", "IN", "OUT"]
    summary = day_summary(developer)
    assert summary.worked_seconds == 7 * 3600  # lunch hour excluded
    assert summary.status == "PRESENT"


def test_toggle_rule_odd_scans_is_incomplete(reader, card, developer, settings):
    settings.ATTENDANCE_DIRECTION_RULE = "toggle"
    for hour in (9, 12, 13):
        scan(reader, card, at(21, hour))
    summary = day_summary(developer)
    assert summary.status == "INCOMPLETE"
    assert summary.worked_seconds == 3 * 3600


def test_device_rule_uses_reader_direction(card, developer, settings):
    settings.ATTENDANCE_DIRECTION_RULE = "device"
    entrance = rfid.register_device(actor=None, code="IN-1", direction=DeviceDirection.IN)[0]
    exit_ = rfid.register_device(actor=None, code="OUT-1", direction=DeviceDirection.OUT)[0]

    scan(entrance, card, at(21, 9))
    scan(entrance, card, at(21, 10))  # entered twice without leaving
    scan(exit_, card, at(21, 17))

    assert types(developer) == ["IN", "IN", "OUT"]
    summary = day_summary(developer)
    assert summary.worked_seconds == 8 * 3600  # from the first IN
    assert summary.status == "INCOMPLETE"


def test_worked_time_ignores_out_without_in():
    worked, complete = worked_time([at(21, 8), at(21, 9), at(21, 17)], ["OUT", "IN", "OUT"])
    assert (worked, complete) == (8 * 3600, False)


# --- Working day boundary ----------------------------------------------------


def test_night_shift_counts_for_start_day(reader, card, developer, settings):
    settings.ATTENDANCE_DAY_START_HOUR = 4
    scan(reader, card, at(21, 22))
    scan(reader, card, at(22, 2))  # after midnight, before 04:00
    summary = day_summary(developer, 21)
    assert summary.record_count == 2
    assert summary.worked_seconds == 4 * 3600


def test_work_date_uses_company_timezone(settings):
    settings.TIME_ZONE = "Asia/Tokyo"  # UTC+9
    timezone.deactivate()
    try:
        assert str(services.work_date_for(at(21, 20))) == "2026-09-22"
    finally:
        settings.TIME_ZONE = "UTC"


# --- Corrections -------------------------------------------------------------


@pytest.fixture
def manager(auth_client, make_user):
    return auth_client(make_user(Roles.MANAGER))


def test_manual_record_completes_the_day(manager, reader, card, developer):
    scan(reader, card, at(21, 9))
    response = manager.post(
        RECORDS,
        {"developer": developer.pk, "event_time": at(21, 18).isoformat(), "note": "Forgot card"},
    )
    assert response.status_code == 201, response.json()
    assert response.json()["source"] == "MANUAL"
    summary = day_summary(developer)
    assert summary.status == "PRESENT" and summary.worked_seconds == 9 * 3600
    assert AuditLog.objects.filter(action="attendance.record_added").exists()


def test_manual_record_requires_note_and_past_time(manager, developer):
    assert (
        manager.post(RECORDS, {"developer": developer.pk, "event_time": at(21, 9)}).status_code
        == 400
    )
    future = (timezone.now() + timedelta(hours=2)).isoformat()
    response = manager.post(RECORDS, {"developer": developer.pk, "event_time": future, "note": "x"})
    assert response.status_code == 400


def test_void_record_updates_summary_and_is_audited(manager, reader, card, developer):
    scan(reader, card, at(21, 9))
    wrong = scan(reader, card, at(21, 11)).attendance_record
    scan(reader, card, at(21, 17))

    response = manager.post(f"{RECORDS}{wrong.pk}/void/", {"reason": "Card used by colleague"})
    assert response.status_code == 200
    assert response.json()["is_void"] is True
    assert day_summary(developer).record_count == 2
    assert AuditLog.objects.filter(action="attendance.record_voided").exists()

    again = manager.post(f"{RECORDS}{wrong.pk}/void/", {"reason": "again"})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "RECORD_ALREADY_VOID"


def test_voiding_only_record_removes_summary(manager, reader, card, developer):
    record = scan(reader, card, at(21, 9)).attendance_record
    manager.post(f"{RECORDS}{record.pk}/void/", {"reason": "Test scan"})
    assert not DailyAttendance.objects.exists()


def test_void_requires_reason(manager, reader, card):
    record = scan(reader, card, at(21, 9)).attendance_record
    assert manager.post(f"{RECORDS}{record.pk}/void/", {}).status_code == 400


# --- API access ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "view", "correct"),
    [
        (Roles.BOSS, 200, 201),
        (Roles.MANAGER, 200, 201),
        (Roles.FINANCE_MANAGER, 403, 403),
        (Roles.DEVELOPER, 403, 403),
        (Roles.SELLER, 403, 403),
    ],
)
def test_access_by_role(auth_client, make_user, developer, role, view, correct):
    client = auth_client(make_user(role))
    assert client.get(DAILY).status_code == view
    assert client.get(RECORDS).status_code == view
    response = client.post(
        RECORDS, {"developer": developer.pk, "event_time": at(21, 9).isoformat(), "note": "x"}
    )
    assert response.status_code == correct


def test_developer_sees_only_own_attendance(auth_client, make_user, reader, card, developer):
    user = make_user(Roles.DEVELOPER)
    developer.user = user
    developer.save()
    other = Developer.objects.create(employee_number="E2", full_name="Bob")
    other_card = RFIDCard.objects.create(uid="0411111111")
    RFIDCardAssignment.objects.create(card=other_card, developer=other)
    scan(reader, card, at(21, 9))
    scan(reader, other_card, at(21, 9))

    client = auth_client(user)
    daily = client.get(f"{DAILY}me/").json()["results"]
    assert [d["developer"]["id"] for d in daily] == [developer.pk]
    records = client.get(f"{RECORDS}me/").json()["results"]
    assert [r["developer"]["id"] for r in records] == [developer.pk]


def test_me_without_profile_is_404(auth_client, make_user):
    response = auth_client(make_user(Roles.SELLER)).get(f"{DAILY}me/")
    assert response.status_code == 404


def test_daily_filters(manager, reader, card, developer):
    scan(reader, card, at(20, 9))
    scan(reader, card, at(21, 9))
    scan(reader, card, at(21, 17))

    def days(query):
        return [d["work_date"] for d in manager.get(f"{DAILY}?{query}").json()["results"]]

    assert days("date_from=2026-09-21") == ["2026-09-21"]
    assert days("status=INCOMPLETE") == ["2026-09-20"]
    assert days("department=nothing") == []
    body = manager.get(f"{DAILY}?date_from=2026-09-21").json()["results"][0]
    assert body["worked_hours"] == 8.0


# --- Rebuild -----------------------------------------------------------------


def test_rebuild_after_rule_change(reader, card, developer, settings):
    for hour in (9, 12, 13, 17):
        scan(reader, card, at(21, hour))
    assert day_summary(developer).worked_seconds == 8 * 3600

    settings.ATTENDANCE_DIRECTION_RULE = "toggle"
    result = services.rebuild()

    assert result["days_recomputed"] == 1
    assert types(developer) == ["IN", "OUT", "IN", "OUT"]
    assert day_summary(developer).worked_seconds == 7 * 3600


def test_rebuild_backfills_missing_records_and_moves_days(reader, card, developer, settings):
    scan(reader, card, at(22, 2))
    AttendanceRecord.objects.all().delete()
    DailyAttendance.objects.all().delete()

    settings.ATTENDANCE_DAY_START_HOUR = 4
    result = services.rebuild()
    assert result["records_created"] == 1
    assert day_summary(developer, 21).record_count == 1

    settings.ATTENDANCE_DAY_START_HOUR = 0
    result = services.rebuild()
    assert result["records_moved"] == 1
    assert day_summary(developer, 22).record_count == 1
    assert not DailyAttendance.objects.filter(work_date="2026-09-21").exists()
