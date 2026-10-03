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
    till, till_key = rfid.register_device(
        actor=None, code="TILL-CAFE-1", purpose="TILL", seller=seller
    )
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


def open_purchase(shop, qty=2, scan=True):
    """A draft with tea on the seller's reader; `scan`: the seller pressed Scan card to buy."""
    client = shop["seller_client"]
    pid = client.post(
        PURCHASES, {"service_position": shop["counter"].pk, "reader": "TILL-CAFE-1"}
    ).json()["id"]
    client.post(f"{PURCHASES}{pid}/items/", {"good": shop["tea"].pk, "quantity": qty})
    if scan:
        wait(shop, pid)
    return pid


def wait(shop, pid):
    r = shop["seller_client"].post(f"{PURCHASES}{pid}/wait/")
    assert r.status_code == 200, r.json()
    return r.json()


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
def test_sellers_list_their_readers_and_switch_them(shop):
    client = shop["seller_client"]
    readers = f"{PURCHASES}readers/?service_position={shop['counter'].pk}"
    assert [r["code"] for r in client.get(readers).json()] == ["TILL-CAFE-1"]
    rfid.register_device(actor=None, code="TILL-2", purpose="TILL", seller=shop["seller"])
    shop_b = Seller.objects.create(name="Shop B")
    rfid.register_device(actor=None, code="TILL-B", purpose="TILL", seller=shop_b)
    assert [r["code"] for r in client.get(readers).json()] == ["TILL-2", "TILL-CAFE-1"]

    pid = open_purchase(shop)
    r = client.post(f"{PURCHASES}{pid}/reader/", {"reader": "TILL-2"})
    assert (r.json()["reader"], r.json()["waiting_for_card"]) == ("TILL-2", False)
    r = client.post(f"{PURCHASES}{pid}/reader/", {"reader": "TILL-B"})  # another seller's
    assert "reader" in r.json()["error"]["details"]
    assert client.post(f"{PURCHASES}{pid}/reader/", {"reader": "Door9"}).status_code == 400
    other_counter = ServicePosition.objects.create(seller=shop_b, name="B counter")
    r = client.get(f"{PURCHASES}readers/?service_position={other_counter.pk}")
    assert r.status_code == 400  # not this seller's counter


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
def test_a_tap_ends_the_wait_and_scanning_again_takes_a_new_card(shop):
    dev1, card1 = make_developer(1)
    dev2, card2 = make_developer(2)
    pid = open_purchase(shop)
    assert tap(shop, card1).json()["purchase"] == pid
    assert tap(shop, card2).json()["purchase"] is None  # nobody is waiting any more
    wait(shop, pid)  # wrong person: the seller scans again
    assert tap(shop, card2).json()["purchase"] == pid
    r = confirm(shop, pid)
    assert r.json()["developer"]["id"] == dev2.pk
    assert DeveloperAccount.objects.get(developer=dev1).balance == Decimal("50.00")


@pytest.mark.django_db
def test_tap_without_open_purchase(shop):
    _, card = make_developer(1)
    body = tap(shop, card).json()
    assert (body["purchase"], body["display_message"]) == (
        None,
        "No purchase is waiting for a card",
    )


@pytest.mark.django_db
def test_tap_only_reaches_purchases_using_that_reader(shop):
    _, card = make_developer(1)
    rfid.register_device(actor=None, code="TILL-2", purpose="TILL", seller=shop["seller"])
    other = ServicePosition.objects.create(seller=shop["seller"], name="Counter 2")
    on_till_2 = shop["seller_client"].post(
        PURCHASES, {"service_position": other.pk, "reader": "TILL-2"}
    )
    wait(shop, on_till_2.json()["id"])
    open_purchase(shop, scan=False)  # on TILL-CAFE-1, but nobody pressed Scan card to buy
    assert tap(shop, card).json()["purchase"] is None


@pytest.mark.django_db
def test_only_the_purchase_waiting_on_the_reader_gets_the_tap(shop, settings):
    """Old drafts never catch taps; scanning on another purchase takes the reader over."""
    _, card = make_developer(1)
    first = open_purchase(shop)
    other = ServicePosition.objects.create(seller=shop["seller"], name="Counter 2")
    second = (
        shop["seller_client"]
        .post(PURCHASES, {"service_position": other.pk, "reader": "TILL-CAFE-1"})
        .json()["id"]
    )
    assert wait(shop, second)["waiting_for_card"] is True
    first_now = shop["seller_client"].get(f"{PURCHASES}{first}/").json()
    assert first_now["waiting_for_card"] is False  # one purchase waits per reader
    assert tap(shop, card).json()["purchase"] == second

    wait(shop, first)  # back at the first purchase
    shop["seller_client"].post(f"{PURCHASES}{first}/stop-waiting/")  # the seller cancels
    assert tap(shop, card).json()["purchase"] is None
    wait(shop, first)
    Purchase.objects.filter(pk=first).update(  # the wait has run out
        waiting_since=timezone.now()
        - timedelta(seconds=settings.PURCHASE_CARD_PRESENTATION_SECONDS + 1)
    )
    assert tap(shop, card).json()["purchase"] is None


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
    assert tap(shop, card).json()["purchase"] == second  # seconds later, still accepted
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
    assert auth_client(make_user(Roles.ADMIN)).post(HEARTBEAT).status_code in (401, 403)


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


@pytest.mark.django_db
def test_seller_with_one_reader_uses_it_and_scanning_needs_a_reader(shop):
    client = shop["seller_client"]
    auto = client.post(PURCHASES, {"service_position": shop["counter"].pk}).json()
    assert auto["reader"] == "TILL-CAFE-1"  # the seller's only reader
    rfid.register_device(actor=None, code="TILL-2", purpose="TILL", seller=shop["seller"])
    pid = client.post(PURCHASES, {"service_position": shop["counter"].pk}).json()["id"]
    r = client.post(f"{PURCHASES}{pid}/wait/")  # two readers: the seller chooses first
    assert r.json()["error"]["code"] == "NO_TILL_READER"
    r = client.post(PURCHASES, {"service_position": shop["counter"].pk, "reader": "Nope"})
    assert r.status_code == 400


@pytest.mark.django_db
def test_only_tills_belong_to_a_seller(auth_client, make_user, shop):
    client = auth_client(make_user(Roles.ADMIN))
    r = client.post(
        "/api/v1/rfid/devices/",
        {"code": "TILL-9", "purpose": "TILL", "seller": shop["seller"].pk},
    )
    assert (r.status_code, r.json()["seller_name"]) == (201, "Cafe")
    r = client.post(
        "/api/v1/rfid/devices/",
        {"code": "Master9", "purpose": "ENROLL", "seller": shop["seller"].pk},
    )
    assert "seller" in r.json()["error"]["details"]
    listed = client.get(f"/api/v1/rfid/devices/?seller={shop['seller'].pk}").json()["results"]
    assert sorted(d["code"] for d in listed) == ["TILL-9", "TILL-CAFE-1"]


@pytest.mark.django_db
def test_till_frame_gets_card_ok_only_when_a_purchase_took_the_tap(shop):
    from apps.rfid.tcp import handle_frame

    _, card = make_developer(1)
    frame = f"ID:ID:TILL-CAFE-1,TYPE:Pay,UID={card.uid}".encode()
    assert handle_frame(frame, "10.0.5.21") == "CARD_NO\r\n"  # nobody is waiting
    pid = open_purchase(shop)
    assert handle_frame(frame, "10.0.5.21") == "CARD_OK\r\n"
    assert Purchase.objects.get(pk=pid).presented_event.uid == card.uid
    wait(shop, pid)
    unknown = b"ID:ID:TILL-CAFE-1,TYPE:Pay,UID=04FFFFFF"
    assert handle_frame(unknown, "10.0.5.21") == "CARD_NO\r\n"
