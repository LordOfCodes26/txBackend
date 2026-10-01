import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.rbac import Roles
from apps.attendance.models import AttendanceRecord
from apps.developers.models import Developer
from apps.finance import services as finance
from apps.finance.models import DeveloperAccount
from apps.goods.models import Good
from apps.purchases.models import Purchase
from apps.rfid import services as rfid
from apps.rfid.models import CardStatus, RFIDCard, RFIDCardAssignment, RFIDDevice, RFIDEvent
from apps.sellers.models import Seller, ServicePosition

PIN = "4826"
EVENTS = "/api/v1/rfid/events/"
HEARTBEAT = "/api/v1/rfid/device/heartbeat/"
PURCHASES = "/api/v1/purchases/"


def key():
    return str(uuid.uuid4())


def device_client(api_key):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Device {api_key}")
    return client


def make_developer(n, balance="50.00"):
    dev = Developer.objects.create(employee_number=f"E{n}", full_name=f"Dev {n}")
    card = RFIDCard.objects.create(uid=f"04AA0000{n:02d}")
    RFIDCardAssignment.objects.create(card=card, developer=dev)
    account = finance.open_account(dev)
    finance.deposit(actor=None, developer=dev, amount=balance, idempotency_key=f"seed-{n:06d}")
    finance.set_pin(actor=None, account=account, pin=PIN, current_pin=None)
    return dev, card


@pytest.fixture
def shop(db, make_user):
    seller_user = make_user(Roles.SELLER, email="cafe@x.com")
    seller = Seller.objects.create(name="Cafe", user=seller_user)
    counter = ServicePosition.objects.create(seller=seller, name="Counter 1")
    tea = Good.objects.create(
        service_position=counter, name="Tea", price="2.50", kind="SERVICE", track_stock=False
    )
    till, till_key = rfid.register_device(actor=None, code="TILL-CAFE-1", purpose="TILL")
    seller_client = APIClient()
    seller_client.force_authenticate(seller_user)
    return {
        "seller": seller,
        "counter": counter,
        "tea": tea,
        "till": till,
        "till_client": device_client(till_key),
        "seller_client": seller_client,
    }


def open_purchase(shop, qty=2):
    client = shop["seller_client"]
    pid = client.post(
        PURCHASES, {"service_position": shop["counter"].pk, "reader": "TILL-CAFE-1"}
    ).json()["id"]
    client.post(f"{PURCHASES}{pid}/items/", {"good": shop["tea"].pk, "quantity": qty})
    return pid


def tap(shop, card):
    return shop["till_client"].post(EVENTS, {"uid": card.uid})


def confirm(shop, pid, **body):
    return shop["seller_client"].post(
        f"{PURCHASES}{pid}/confirm/", {"pin": PIN, **body}, HTTP_IDEMPOTENCY_KEY=key()
    )


# --- Device registration ---------------------------------------------------------------


@pytest.mark.django_db
def test_till_devices_have_no_counter(auth_client, make_user, shop):
    client = auth_client(make_user(Roles.MANAGER))
    r = client.post("/api/v1/rfid/devices/", {"code": "TILL-2", "purpose": "TILL"})
    assert r.status_code == 201 and r.json()["api_key"]
    assert "service_position" not in r.json()


@pytest.mark.django_db
def test_sellers_list_readers_and_switch_them(shop):
    client = shop["seller_client"]
    codes = [r["code"] for r in client.get(f"{PURCHASES}readers/").json()]
    assert codes == ["TILL-CAFE-1"]
    rfid.register_device(actor=None, code="TILL-2", purpose="TILL")
    pid = open_purchase(shop)
    r = client.post(f"{PURCHASES}{pid}/reader/", {"reader": "TILL-2"})
    assert r.json()["reader"] == "TILL-2"
    assert client.post(f"{PURCHASES}{pid}/reader/", {"reader": "Door9"}).status_code == 400


# --- Till flow -------------------------------------------------------------------------


@pytest.mark.django_db
def test_tap_is_presented_to_open_purchase_and_confirmed(shop):
    dev, card = make_developer(1)
    pid = open_purchase(shop)

    r = tap(shop, card)
    assert r.status_code == 201
    body = r.json()
    assert (body["result"], body["purchase"]) == ("ACCEPTED", pid)
    assert body["display_message"] == "Dev 1 - enter PIN"
    assert not AttendanceRecord.objects.exists()  # till taps are not attendance

    seen = shop["seller_client"].get(f"{PURCHASES}{pid}/").json()["presented_card"]
    assert seen["developer"]["full_name"] == "Dev 1"

    r = confirm(shop, pid)
    assert r.status_code == 201, r.json()
    assert (r.json()["card_uid"], r.json()["total"]) == (card.uid, "5.00")
    assert DeveloperAccount.objects.get(developer=dev).balance == Decimal("45.00")
    assert shop["seller_client"].get(f"{PURCHASES}{pid}/").json()["presented_card"] is None


@pytest.mark.django_db
def test_confirm_without_tap_is_rejected(shop):
    pid = open_purchase(shop)
    assert confirm(shop, pid).json()["error"]["code"] == "CARD_NOT_PRESENTED"


@pytest.mark.django_db
def test_presentation_expires(shop, settings):
    _, card = make_developer(1)
    pid = open_purchase(shop)
    tap(shop, card)
    Purchase.objects.filter(pk=pid).update(
        presented_at=timezone.now()
        - timedelta(seconds=settings.PURCHASE_CARD_PRESENTATION_SECONDS + 1)
    )
    assert confirm(shop, pid).json()["error"]["code"] == "CARD_NOT_PRESENTED"
    assert shop["seller_client"].get(f"{PURCHASES}{pid}/").json()["presented_card"] is None


@pytest.mark.django_db
def test_typed_card_uid_is_refused_by_default(shop):
    _, card = make_developer(1)
    pid = open_purchase(shop)
    r = confirm(shop, pid, card_uid=card.uid)
    assert (r.status_code, r.json()["error"]["code"]) == (403, "MANUAL_CARD_ENTRY_DISABLED")


@pytest.mark.django_db
def test_newest_tap_wins(shop):
    dev1, card1 = make_developer(1)
    dev2, card2 = make_developer(2)
    pid = open_purchase(shop)
    tap(shop, card1)
    tap(shop, card2)
    r = confirm(shop, pid)
    assert r.json()["developer"]["id"] == dev2.pk
    assert DeveloperAccount.objects.get(developer=dev1).balance == Decimal("50.00")


@pytest.mark.django_db
def test_tap_without_open_purchase(shop):
    _, card = make_developer(1)
    body = tap(shop, card).json()
    assert (body["purchase"], body["display_message"]) == (None, "No open purchase for this reader")


@pytest.mark.django_db
def test_tap_only_reaches_purchases_using_that_reader(shop):
    _, card = make_developer(1)
    rfid.register_device(actor=None, code="TILL-2", purpose="TILL")
    other = ServicePosition.objects.create(seller=shop["seller"], name="Counter 2")
    shop["seller_client"].post(PURCHASES, {"service_position": other.pk, "reader": "TILL-2"})
    shop["seller_client"].post(PURCHASES, {"service_position": other.pk})  # no reader
    assert tap(shop, card).json()["purchase"] is None


@pytest.mark.django_db
def test_reader_moved_to_another_pc(shop):
    """The reader is plugged into another PC: that PC's purchase now gets the taps."""
    _, card = make_developer(1)
    first = open_purchase(shop)
    other = ServicePosition.objects.create(seller=shop["seller"], name="Counter 2")
    second = (
        shop["seller_client"]
        .post(PURCHASES, {"service_position": other.pk, "reader": "TILL-CAFE-1"})
        .json()["id"]
    )
    assert tap(shop, card).json()["purchase"] == second
    # Moved back: the first PC selects the reader again for its open purchase.
    shop["seller_client"].post(f"{PURCHASES}{second}/cancel/")
    shop["seller_client"].post(f"{PURCHASES}{first}/reader/", {"reader": "TILL-CAFE-1"})
    assert tap(shop, card).json()["purchase"] == first


@pytest.mark.django_db
def test_blocked_card_is_not_presented(shop):
    _, card = make_developer(1)
    RFIDCard.objects.filter(pk=card.pk).update(status=CardStatus.BLOCKED)
    pid = open_purchase(shop)
    body = tap(shop, card).json()
    assert (body["result"], body["purchase"], body["display_message"]) == (
        "BLOCKED_CARD",
        None,
        "Card blocked",
    )
    assert confirm(shop, pid).json()["error"]["code"] == "CARD_NOT_PRESENTED"


@pytest.mark.django_db
def test_till_taps_are_not_debounced(shop):
    dev, card = make_developer(1)
    first = open_purchase(shop)
    tap(shop, card)
    assert confirm(shop, first).status_code == 201
    second = open_purchase(shop)
    assert tap(shop, card).json()["result"] == "ACCEPTED"  # seconds later, still accepted
    assert confirm(shop, second).status_code == 201
    assert DeveloperAccount.objects.get(developer=dev).balance == Decimal("40.00")


# --- Heartbeat ----------------------------------------------------------------------------


@pytest.mark.django_db
def test_heartbeat_reports_config_and_marks_online(auth_client, make_user, shop):
    r = shop["till_client"].post(
        HEARTBEAT, {"app_version": "till-agent 1.2.0"}, REMOTE_ADDR="10.0.0.42"
    )
    assert r.status_code == 200
    body = r.json()
    assert (body["code"], body["purpose"]) == ("TILL-CAFE-1", "TILL")
    assert "service_position" not in body
    assert abs(timezone.datetime.fromisoformat(body["server_time"]) - timezone.now()) < timedelta(
        seconds=5
    )
    assert body["heartbeat_seconds"] == 30

    manager = auth_client(make_user(Roles.MANAGER))
    device = manager.get(f"/api/v1/rfid/devices/{shop['till'].pk}/").json()
    assert (device["online"], device["app_version"], device["last_ip"]) == (
        True,
        "till-agent 1.2.0",
        "10.0.0.42",
    )

    RFIDDevice.objects.filter(pk=shop["till"].pk).update(
        last_seen_at=timezone.now() - timedelta(minutes=10)
    )
    offline = manager.get("/api/v1/rfid/devices/?online=false").json()["results"]
    assert [d["code"] for d in offline] == ["TILL-CAFE-1"]


@pytest.mark.django_db
def test_heartbeat_requires_device_key(auth_client, make_user):
    assert APIClient().post(HEARTBEAT).status_code == 401
    assert auth_client(make_user(Roles.BOSS)).post(HEARTBEAT).status_code in (401, 403)


# --- Batch upload ---------------------------------------------------------------------------


@pytest.fixture
def entrance(db):
    device, api_key = rfid.register_device(actor=None, code="READER-001")
    return device, device_client(api_key)


@pytest.mark.django_db
def test_batch_upload_of_buffered_scans(entrance):
    dev, card = make_developer(1)
    _, client = entrance
    t0 = timezone.now() - timedelta(hours=3)
    events = [  # sent out of order on purpose
        {
            "uid": card.uid,
            "event_time": (t0 + timedelta(hours=1)).isoformat(),
            "client_event_id": "r1-0003",
        },
        {"uid": card.uid, "event_time": t0.isoformat(), "client_event_id": "r1-0001"},
        {
            "uid": card.uid,
            "event_time": (t0 + timedelta(seconds=3)).isoformat(),
            "client_event_id": "r1-0002",
        },
    ]
    r = client.post(f"{EVENTS}batch/", {"events": events}, format="json")
    assert r.status_code == 200, r.json()
    assert [(x["client_event_id"], x["result"], x["created"]) for x in r.json()] == [
        ("r1-0001", "ACCEPTED", True),
        ("r1-0002", "DUPLICATE", True),
        ("r1-0003", "ACCEPTED", True),
    ]
    assert AttendanceRecord.objects.filter(developer=dev).count() == 2

    again = client.post(f"{EVENTS}batch/", {"events": events}, format="json").json()
    assert all(x["created"] is False for x in again)
    assert RFIDEvent.objects.count() == 3


@pytest.mark.django_db
@pytest.mark.parametrize(
    "events",
    [
        [],
        [{"uid": "04AA000001", "event_time": "2026-01-01T00:00:00Z"}],  # no client_event_id
        [{"uid": "04AA000001", "event_time": "2999-01-01T00:00:00Z", "client_event_id": "a1"}],
        [
            {"uid": "04AA000001", "event_time": "2026-01-01T00:00:00Z", "client_event_id": "a1"},
            {"uid": "04AA000001", "event_time": "2026-01-01T00:01:00Z", "client_event_id": "a1"},
        ],
    ],
)
def test_batch_validation(entrance, events):
    _, client = entrance
    assert client.post(f"{EVENTS}batch/", {"events": events}, format="json").status_code == 400


@pytest.mark.django_db
def test_till_devices_cannot_batch_upload(shop):
    r = shop["till_client"].post(
        f"{EVENTS}batch/",
        {
            "events": [
                {
                    "uid": "04AA000001",
                    "event_time": timezone.now().isoformat(),
                    "client_event_id": "x1",
                }
            ]
        },
        format="json",
    )
    assert r.status_code == 400
