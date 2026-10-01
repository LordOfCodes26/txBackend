from datetime import timedelta

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.accounts.rbac import Roles
from apps.audit.models import AuditLog
from apps.developers.models import Developer, DeveloperStatus
from apps.rfid import services
from apps.rfid.models import CardStatus, RFIDCard, RFIDCardAssignment, RFIDDevice, RFIDEvent

pytestmark = pytest.mark.django_db

EVENTS = "/api/v1/rfid/events/"
DEVICES = "/api/v1/rfid/devices/"


@pytest.fixture
def device_and_key(db):
    return services.register_device(actor=None, code="READER-001", name="Front door")


@pytest.fixture
def reader(api_client, device_and_key):
    api_client.credentials(HTTP_AUTHORIZATION=f"Device {device_and_key[1]}")
    return api_client


@pytest.fixture
def developer(db):
    return Developer.objects.create(employee_number="E1", full_name="Ada")


@pytest.fixture
def card(developer):
    card = RFIDCard.objects.create(uid="04AABBCCDD")
    RFIDCardAssignment.objects.create(card=card, developer=developer)
    return card


def scan(client, uid="04AABBCCDD", **extra):
    return client.post(EVENTS, {"uid": uid, **extra})


def test_accepted_scan(reader, card, developer, device_and_key):
    response = scan(reader, uid="04:aa:bb:cc:dd", device_id="READER-001")
    assert response.status_code == 201, response.json()
    body = response.json()
    assert body["result"] == "ACCEPTED" and body["accepted"] is True
    assert body["developer"]["full_name"] == "Ada"

    event = RFIDEvent.objects.get()
    assert (event.card, event.developer, event.uid) == (card, developer, "04AABBCCDD")
    device_and_key[0].refresh_from_db()
    assert device_and_key[0].last_seen_at is not None


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("unknown", "UNKNOWN_CARD"),
        ("unassigned", "UNASSIGNED_CARD"),
        ("blocked", "BLOCKED_CARD"),
        ("retired", "RETIRED_CARD"),
        ("suspended", "INACTIVE_DEVELOPER"),
        ("on_leave", "ACCEPTED"),
    ],
)
def test_scan_results(reader, card, developer, setup, expected):
    uid = card.uid
    if setup == "unknown":
        uid = "0411223344"
    elif setup == "unassigned":
        RFIDCardAssignment.objects.update(unassigned_at=timezone.now())
    elif setup in ("blocked", "retired"):
        card.status = CardStatus[setup.upper()]
        card.save()
    elif setup in ("suspended", "on_leave"):
        developer.status = DeveloperStatus[setup.upper()]
        developer.save()

    body = scan(reader, uid=uid).json()
    assert body["result"] == expected
    assert body["accepted"] is (expected == "ACCEPTED")


def test_rejected_scans_are_still_stored(reader):
    scan(reader, uid="0411223344")
    assert RFIDEvent.objects.get().result == "UNKNOWN_CARD"


def test_repeat_scans_are_debounced(reader, card, settings):
    settings.RFID_DEBOUNCE_SECONDS = 10
    t0 = timezone.now() - timedelta(minutes=5)
    results = [
        scan(reader, event_time=(t0 + timedelta(seconds=s)).isoformat()).json()["result"]
        for s in (0, 1, 2, 30)
    ]
    assert results == ["ACCEPTED", "DUPLICATE", "DUPLICATE", "ACCEPTED"]
    assert RFIDEvent.objects.count() == 4


def test_late_buffered_scan_is_debounced_against_newer_one(reader, card):
    t0 = timezone.now() - timedelta(minutes=5)
    scan(reader, event_time=t0.isoformat())
    earlier = scan(reader, event_time=(t0 - timedelta(seconds=3)).isoformat()).json()
    assert earlier["result"] == "DUPLICATE"


def test_retry_with_same_client_event_id_is_idempotent(reader, card):
    first = scan(reader, client_event_id="r1-000042")
    again = scan(reader, client_event_id="r1-000042")
    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json()["id"] == again.json()["id"]
    assert RFIDEvent.objects.count() == 1


def test_future_event_time_rejected(reader, card):
    response = scan(reader, event_time=(timezone.now() + timedelta(hours=1)).isoformat())
    assert response.status_code == 400
    assert "event_time" in response.json()["error"]["details"]


def test_device_id_must_match_key(reader, card):
    response = scan(reader, device_id="READER-999")
    assert response.status_code == 400


def test_bad_or_inactive_device_key_is_rejected(api_client, device_and_key, card):
    api_client.credentials(HTTP_AUTHORIZATION="Device not-a-real-key")
    assert scan(api_client).status_code == 401

    device, key = device_and_key
    device.is_active = False
    device.save()
    api_client.credentials(HTTP_AUTHORIZATION=f"Device {key}")
    assert scan(api_client).status_code == 401


def test_users_cannot_submit_scans_and_devices_cannot_read(
    auth_client, make_user, reader, card, api_client
):
    assert reader.get(EVENTS).status_code == 403
    api_client.credentials()
    boss = auth_client(make_user(Roles.BOSS))
    assert scan(boss).status_code == 403


def test_staff_can_list_and_filter_events(auth_client, make_user, reader, card):
    scan(reader)
    scan(reader, uid="0411223344")
    reader.credentials()
    client = auth_client(make_user(Roles.MANAGER))
    results = client.get(f"{EVENTS}?result=UNKNOWN_CARD").json()["results"]
    assert [e["uid"] for e in results] == ["0411223344"]
    assert client.get(f"{EVENTS}?uid=04:aa:bb:cc:dd").json()["count"] == 1


def test_events_are_append_only(reader, card):
    scan(reader)
    with pytest.raises(IntegrityError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("UPDATE rfid_rfidevent SET result = 'ACCEPTED'")


def test_register_device_returns_key_once(auth_client, make_user):
    client = auth_client(make_user(Roles.MANAGER))
    created = client.post(DEVICES, {"code": "READER-002", "location": "Lobby"})
    assert created.status_code == 201
    key = created.json()["api_key"]
    assert len(key) > 30

    detail = client.get(f"{DEVICES}{created.json()['id']}/").json()
    assert "api_key" not in detail
    device = RFIDDevice.objects.get(code="READER-002")
    assert key not in (device.api_key_hash, device.api_key_prefix) and device.check_api_key(key)
    assert AuditLog.objects.filter(action="rfid.device_registered").exists()


def test_rotate_key_invalidates_old_key(auth_client, make_user, api_client, device_and_key, card):
    device, old_key = device_and_key
    manager = auth_client(make_user(Roles.MANAGER))
    new_key = manager.post(f"{DEVICES}{device.pk}/rotate-key/").json()["api_key"]
    manager.force_authenticate(None)

    api_client.credentials(HTTP_AUTHORIZATION=f"Device {old_key}")
    assert scan(api_client).status_code == 401
    api_client.credentials(HTTP_AUTHORIZATION=f"Device {new_key}")
    assert scan(api_client).status_code == 201
