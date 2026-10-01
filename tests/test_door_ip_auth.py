"""Door devices without API keys authenticate by fixed IP + their ID in the body."""

from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.rbac import Roles
from apps.attendance.occupancy import occupancy
from apps.developers.models import Developer
from apps.rfid import services as rfid
from apps.rfid.models import Building, RFIDCard, RFIDCardAssignment, RFIDDevice, RFIDEvent

pytestmark = pytest.mark.django_db
EVENTS = "/api/v1/rfid/events/"
DOOR1_IP, DOOR2_IP = "10.20.0.11", "10.20.0.12"


@pytest.fixture(autouse=True)
def device_rule(settings):
    settings.ATTENDANCE_DIRECTION_RULE = "device"


@pytest.fixture
def doors():
    b1 = Building.objects.create(code="B1", name="Building 1")
    b2 = Building.objects.create(code="B2", name="Building 2")
    door1, door1_key = rfid.register_device(
        actor=None, code="Door1", building=b1, allowed_ip=DOOR1_IP
    )
    door2, _ = rfid.register_device(actor=None, code="Door2", building=b2, allowed_ip=DOOR2_IP)
    dev = Developer.objects.create(employee_number="E1", full_name="Ada Lovelace")
    RFIDCardAssignment.objects.create(card=RFIDCard.objects.create(uid="04A2B3C4"), developer=dev)
    return {"door1": door1, "door1_key": door1_key, "door2": door2}


def post_from(ip, payload, path=EVENTS, **extra):
    return APIClient().post(path, payload, format="json", REMOTE_ADDR=ip, **extra)


def test_door_scan_from_its_ip_without_key(doors):
    r = post_from(DOOR1_IP, {"ID": "Door1", "Type": "in", "UID": "04A2B3C4"})
    assert r.status_code == 201, r.json()
    assert (r.json()["result"], r.json()["display_message"]) == (
        "ACCEPTED",
        "Welcome, Ada Lovelace",
    )
    assert RFIDEvent.objects.get().device == doors["door1"]
    assert {b["code"]: b["count"] for b in occupancy()["buildings"]} == {"B1": 1, "B2": 0}


@pytest.mark.parametrize(
    ("ip", "door_id"),
    [
        ("10.20.0.99", "Door1"),  # unknown IP
        (DOOR2_IP, "Door1"),  # Door2's IP claiming to be Door1
        (DOOR1_IP, "Door9"),  # unknown device
    ],
)
def test_wrong_ip_or_id_is_refused(doors, ip, door_id):
    r = post_from(ip, {"ID": door_id, "Type": "in", "UID": "04A2B3C4"})
    assert r.status_code == 401
    assert not RFIDEvent.objects.exists()


def test_request_without_id_is_refused(doors):
    r = post_from(DOOR1_IP, {"Type": "in", "UID": "04A2B3C4"})
    assert r.status_code in (401, 403)
    assert not RFIDEvent.objects.exists()


def test_forwarded_for_header_cannot_be_spoofed(doors, settings):
    # Without a trusted proxy the X-Forwarded-For header is ignored entirely.
    settings.TRUST_X_FORWARDED_FOR = False
    r = post_from(
        "203.0.113.7",
        {"ID": "Door1", "Type": "in", "UID": "04A2B3C4"},
        HTTP_X_FORWARDED_FOR=DOOR1_IP,
    )
    assert r.status_code == 401


def test_behind_nginx_the_proxy_supplied_ip_is_used(doors, settings):
    # nginx overwrites X-Forwarded-For with the real client address.
    settings.TRUST_X_FORWARDED_FOR = True
    r = post_from(
        "127.0.0.1", {"ID": "Door1", "Type": "in", "UID": "04A2B3C4"}, HTTP_X_FORWARDED_FOR=DOOR1_IP
    )
    assert r.status_code == 201


def test_inactive_door_is_refused(doors):
    RFIDDevice.objects.filter(pk=doors["door1"].pk).update(is_active=False)
    assert post_from(DOOR1_IP, {"ID": "Door1", "Type": "in", "UID": "04A2B3C4"}).status_code == 401


def test_key_still_works_and_takes_precedence(doors):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Device {doors['door1_key']}")
    r = client.post(
        EVENTS,
        {"ID": "Door1", "Type": "out", "UID": "04A2B3C4"},
        format="json",
        REMOTE_ADDR="10.99.99.99",
    )
    assert r.status_code == 201
    # A wrong key is not rescued by the IP.
    client.credentials(HTTP_AUTHORIZATION="Device wrong-key")
    r = client.post(
        EVENTS,
        {"ID": "Door1", "Type": "in", "UID": "04A2B3C4"},
        format="json",
        REMOTE_ADDR=DOOR1_IP,
    )
    assert r.status_code == 401


def test_heartbeat_and_batch_by_ip(doors):
    r = post_from(DOOR2_IP, {"ID": "Door2"}, path="/api/v1/rfid/device/heartbeat/")
    assert (r.status_code, r.json()["code"]) == (200, "Door2")
    t = timezone.now() - timedelta(hours=1)
    r = post_from(
        DOOR2_IP,
        {
            "ID": "Door2",
            "events": [
                {
                    "UID": "04A2B3C4",
                    "Type": "in",
                    "event_time": t.isoformat(),
                    "client_event_id": "d2-1",
                }
            ],
        },
        path=f"{EVENTS}batch/",
    )
    assert r.status_code == 200, r.json()
    assert r.json()[0]["result"] == "ACCEPTED"


def test_only_attendance_devices_may_use_ip(auth_client, make_user, doors):

    client = auth_client(make_user(Roles.MANAGER))
    r = client.post(
        "/api/v1/rfid/devices/",
        {
            "code": "TILL-9",
            "purpose": "TILL",
            "allowed_ip": "10.20.0.50",
        },
    )
    assert "allowed_ip" in r.json()["error"]["details"]
    with pytest.raises(IntegrityError), transaction.atomic():
        RFIDDevice.objects.create(
            code="T",
            purpose="TILL",
            allowed_ip="10.20.0.51",
            api_key_hash="x",
            api_key_prefix="x",
        )


def test_setting_the_ip_is_audited(auth_client, make_user, doors):
    from apps.audit.models import AuditLog

    client = auth_client(make_user(Roles.MANAGER))
    r = client.patch(f"/api/v1/rfid/devices/{doors['door1'].pk}/", {"allowed_ip": "10.20.0.21"})
    assert r.json()["allowed_ip"] == "10.20.0.21"
    log = AuditLog.objects.filter(action="rfid.device_updated").latest("id")
    assert (log.old_values, log.new_values) == (
        {"allowed_ip": DOOR1_IP},
        {"allowed_ip": "10.20.0.21"},
    )
