"""The two buildings' door devices send {"ID": "Door1"|"Door2", "Type": "in"|"out", "UID"}."""

from datetime import datetime, time, timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.attendance.models import AttendanceRecord, DailyAttendance
from apps.attendance.services import rebuild
from apps.developers.models import Developer
from apps.rfid import services as rfid
from apps.rfid.models import RFIDCard, RFIDCardAssignment, RFIDEvent

pytestmark = pytest.mark.django_db
EVENTS = "/api/v1/rfid/events/"


@pytest.fixture(autouse=True)
def reported_direction_rule(settings):
    settings.ATTENDANCE_DIRECTION_RULE = "device"


@pytest.fixture
def doors():
    clients = {}
    for code, building in (("Door1", "Building 1"), ("Door2", "Building 2")):
        _, key = rfid.register_device(actor=None, code=code, location=building)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Device {key}")
        clients[code] = client
    return clients


@pytest.fixture
def ada():
    dev = Developer.objects.create(employee_number="E1", full_name="Ada Lovelace")
    card = RFIDCard.objects.create(uid="04A2B3C4")
    RFIDCardAssignment.objects.create(card=card, developer=dev)
    return dev


def yesterday_8am():
    """A fixed time so a test's scans never straddle midnight."""
    day = timezone.localdate() - timedelta(days=1)
    return timezone.make_aware(datetime.combine(day, time(8)))


def door_scan(client, door, kind, uid="04A2B3C4", **extra):
    return client.post(EVENTS, {"ID": door, "Type": kind, "UID": uid, **extra}, format="json")


def test_door_payload_is_accepted_as_sent(doors, ada):
    r = door_scan(doors["Door1"], "Door1", "in")
    assert r.status_code == 201, r.json()
    body = r.json()
    assert (body["result"], body["direction"], body["display_message"]) == (
        "ACCEPTED",
        "IN",
        "Welcome, Ada Lovelace",
    )
    r = door_scan(
        doors["Door1"],
        "Door1",
        "OUT",
        event_time=(timezone.now() + timedelta(seconds=1)).isoformat(),
    )
    assert (r.json()["direction"], r.json()["display_message"]) == ("OUT", "Goodbye, Ada Lovelace")


def test_lowercase_keys_and_values_work(doors, ada):
    r = doors["Door2"].post(
        EVENTS, {"id": "door2", "type": "In", "uid": "04:a2:b3:c4"}, format="json"
    )
    assert r.status_code == 201
    assert RFIDEvent.objects.get().direction == "IN"


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"ID": "Door2", "Type": "in", "UID": "04A2B3C4"}, "device_id"),  # wrong door for key
        ({"ID": "Door1", "Type": "sideways", "UID": "04A2B3C4"}, "direction"),
        ({"ID": "Door1", "Type": "in"}, "uid"),
    ],
)
def test_invalid_door_payloads(doors, ada, payload, field):
    r = doors["Door1"].post(EVENTS, payload, format="json")
    assert r.status_code == 400
    assert field in r.json()["error"]["details"]


def test_attendance_uses_reported_direction_across_buildings(doors, ada):
    base = yesterday_8am()

    def at(hours, door, kind):
        door_scan(doors[door], door, kind, event_time=(base + timedelta(hours=hours)).isoformat())

    at(0, "Door1", "in")  # arrive at building 1
    at(3, "Door1", "out")  # walk over...
    at(3.1, "Door2", "in")  # ...to building 2
    at(8, "Door2", "out")  # leave from building 2

    records = AttendanceRecord.objects.filter(developer=ada).order_by("event_time")
    assert [r.event_type for r in records] == ["IN", "OUT", "IN", "OUT"]
    assert [r.device.code for r in records] == ["Door1", "Door1", "Door2", "Door2"]
    day = DailyAttendance.objects.get(developer=ada)
    assert day.status == "PRESENT"
    assert day.worked_seconds == int((3 + 4.9) * 3600)


def test_quick_in_then_out_is_not_a_duplicate(doors, ada):
    now = timezone.now() - timedelta(minutes=5)
    door_scan(doors["Door1"], "Door1", "in", event_time=now.isoformat())
    r = door_scan(
        doors["Door1"], "Door1", "out", event_time=(now + timedelta(seconds=4)).isoformat()
    )
    assert r.json()["result"] == "ACCEPTED"
    r = door_scan(
        doors["Door1"], "Door1", "out", event_time=(now + timedelta(seconds=6)).isoformat()
    )
    assert r.json()["result"] == "DUPLICATE"  # same direction repeated within 10 s


def test_two_ins_in_a_row_make_the_day_incomplete(doors, ada):
    base = yesterday_8am()
    door_scan(doors["Door1"], "Door1", "in", event_time=base.isoformat())
    door_scan(doors["Door2"], "Door2", "in", event_time=(base + timedelta(hours=1)).isoformat())
    assert DailyAttendance.objects.get(developer=ada).status == "INCOMPLETE"


def test_rebuild_applies_reported_directions_to_existing_days(doors, ada, settings):
    settings.ATTENDANCE_DIRECTION_RULE = "none"
    base = yesterday_8am()
    door_scan(doors["Door1"], "Door1", "in", event_time=base.isoformat())
    door_scan(doors["Door1"], "Door1", "out", event_time=(base + timedelta(hours=5)).isoformat())
    assert set(AttendanceRecord.objects.values_list("event_type", flat=True)) == {"SCAN"}

    settings.ATTENDANCE_DIRECTION_RULE = "device"
    rebuild()
    types = list(
        AttendanceRecord.objects.order_by("event_time").values_list("event_type", flat=True)
    )
    assert types == ["IN", "OUT"]


def test_batch_upload_keeps_directions(doors, ada):
    base = timezone.now() - timedelta(hours=2)
    events = [
        {
            "UID": "04A2B3C4",
            "Type": "out",
            "event_time": (base + timedelta(hours=1)).isoformat(),
            "client_event_id": "d1-2",
        },
        {
            "UID": "04A2B3C4",
            "Type": "in",
            "event_time": base.isoformat(),
            "client_event_id": "d1-1",
        },
    ]
    r = doors["Door1"].post(f"{EVENTS}batch/", {"events": events}, format="json")
    assert r.status_code == 200, r.json()
    assert list(RFIDEvent.objects.order_by("event_time").values_list("direction", flat=True)) == [
        "IN",
        "OUT",
    ]
