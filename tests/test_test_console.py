import pytest

from apps.accounts.rbac import Roles
from apps.developers.models import Developer
from apps.rfid import services as rfid
from apps.rfid.models import Building, RFIDCard, RFIDCardAssignment, RFIDEvent

pytestmark = pytest.mark.django_db
SCAN = "/api/v1/test-console/door-scan/"


@pytest.fixture
def door(settings):
    settings.TEST_CONSOLE_ENABLED = True
    settings.ATTENDANCE_DIRECTION_RULE = "device"
    b1 = Building.objects.create(code="B1", name="Building 1")
    door, _ = rfid.register_device(actor=None, code="Door1", building=b1, allowed_ip="10.0.0.1")
    dev = Developer.objects.create(employee_number="E1", full_name="Ada Lovelace")
    RFIDCardAssignment.objects.create(card=RFIDCard.objects.create(uid="04A2B3C4"), developer=dev)
    return door


def test_page_is_served_without_external_assets(client, door):
    r = client.get("/test-console/")
    assert r.status_code == 200
    html = r.content.decode()
    assert "Test Console" in html and "<script src" not in html and "https://" not in html


def test_simulated_door_scan_uses_the_real_pipeline(auth_client, make_user, door):
    client = auth_client(make_user(Roles.MANAGER))
    r = client.post(SCAN, {"door": "Door1", "type": "in", "uid": "04:a2:b3:c4"})
    assert r.status_code == 201, r.json()
    assert (r.json()["result"], r.json()["display_message"]) == (
        "ACCEPTED",
        "Welcome, Ada Lovelace",
    )
    event = RFIDEvent.objects.get()
    assert (event.device, event.direction, event.attendance_record.event_type) == (door, "IN", "IN")

    bad = client.post(SCAN, {"door": "Door1", "type": "pay", "uid": "04A2B3C4"})
    assert bad.status_code == 400
    assert client.post(SCAN, {"door": "Door9", "type": "in", "uid": "04A2B3C4"}).status_code == 404


def test_simulated_scan_needs_device_permission(auth_client, make_user, door):
    client = auth_client(make_user(Roles.FINANCE_MANAGER))
    assert client.post(SCAN, {"door": "Door1", "type": "in", "uid": "04A2B3C4"}).status_code == 403


def test_console_is_hidden_when_disabled(client, auth_client, make_user, door, settings):
    settings.TEST_CONSOLE_ENABLED = False
    assert client.get("/test-console/").status_code == 404
    r = auth_client(make_user(Roles.BOSS)).post(
        SCAN, {"door": "Door1", "type": "in", "uid": "04A2B3C4"}
    )
    assert r.status_code == 404
