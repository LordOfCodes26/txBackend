"""Till readers are matched to the seller's PC by network address."""

import json
import uuid
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.accounts.rbac import Roles
from apps.developers.models import Developer
from apps.finance import services as finance
from apps.finance.models import DeveloperAccount
from apps.goods.models import Good
from apps.purchases.models import Purchase
from apps.rfid import services as rfid
from apps.rfid.models import RFIDCard, RFIDCardAssignment, RFIDDevice
from apps.rfid.tcp import handle_frame
from apps.sellers.models import Seller, ServicePosition

pytestmark = pytest.mark.django_db
PC_A, PC_B = "10.0.5.21", "10.0.5.22"
PIN = "4826"


@pytest.fixture
def shop(make_user):
    seller_user = make_user(Roles.SELLER)
    seller = Seller.objects.create(name="Cafe", user=seller_user)
    counter = ServicePosition.objects.create(seller=seller, name="Counter 1")
    tea = Good.objects.create(
        service_position=counter, name="Tea", price="2.50", kind="SERVICE", track_stock=False
    )
    rfid.register_device(actor=None, code="Reader1", purpose="TILL", sn="SN-1")
    rfid.register_device(actor=None, code="Reader2", purpose="TILL", sn="SN-2")
    dev = Developer.objects.create(employee_number="E1", full_name="Ada Lovelace")
    RFIDCardAssignment.objects.create(card=RFIDCard.objects.create(uid="04A2B3C4"), developer=dev)
    account = finance.open_account(dev)
    finance.deposit(actor=None, developer=dev, amount="10.00", idempotency_key="seed-000001")
    finance.set_pin(actor=None, account=account, pin=PIN, current_pin=None)
    client = APIClient()
    client.force_authenticate(seller_user)
    return {"client": client, "counter": counter, "tea": tea, "dev": dev}


def new_purchase(shop, ip, **extra):
    r = shop["client"].post(
        "/api/v1/purchases/", {"service_position": shop["counter"].pk, **extra}, REMOTE_ADDR=ip
    )
    assert r.status_code == 201, r.json()
    return r.json()


def tap(reader, sn, ip):
    return (
        APIClient()
        .post(
            "/api/v1/rfid/events/",
            {"SN": sn, "ID": reader, "TYPE": "pay", "UID": "04A2B3C4"},
            format="json",
            REMOTE_ADDR=ip,
        )
        .json()
    )


def test_tap_from_the_same_pc_pays_its_purchase(shop):
    p = new_purchase(shop, PC_A)
    assert (p["reader"], Purchase.objects.get(pk=p["id"]).client_ip) == (None, PC_A)
    shop["client"].post(f"/api/v1/purchases/{p['id']}/items/", {"good": shop["tea"].pk})

    body = tap("Reader1", "SN-1", PC_A)
    assert (body["purchase"], body["display_message"]) == (p["id"], "Ada Lovelace - enter PIN")
    purchase = Purchase.objects.get(pk=p["id"])
    assert purchase.reader.code == "Reader1"  # remembered after the first match
    assert RFIDDevice.objects.get(code="Reader1").last_ip == PC_A

    r = shop["client"].post(
        f"/api/v1/purchases/{p['id']}/confirm/",
        {"pin": PIN},
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert r.status_code == 201
    assert DeveloperAccount.objects.get(developer=shop["dev"]).balance == Decimal("7.50")


def test_tap_from_another_pc_does_not_match(shop):
    new_purchase(shop, PC_A)
    assert tap("Reader2", "SN-2", PC_B)["purchase"] is None


def test_each_pc_gets_its_own_readers_taps(shop):
    a = new_purchase(shop, PC_A)
    b = new_purchase(shop, PC_B)
    assert tap("Reader1", "SN-1", PC_A)["purchase"] == a["id"]
    assert tap("Reader2", "SN-2", PC_B)["purchase"] == b["id"]


def test_explicit_reader_wins_over_address(shop):
    by_ip = new_purchase(shop, PC_A)
    named = new_purchase(shop, PC_B, reader="Reader1")
    assert tap("Reader1", "SN-1", PC_A)["purchase"] == named["id"]
    assert by_ip["id"] != named["id"]


def test_reader_is_detected_before_the_first_purchase_tap(shop):
    client = shop["client"]
    r = client.get("/api/v1/purchases/detected-reader/", REMOTE_ADDR=PC_A).json()
    assert (r["ip"], r["reader"], r["candidates"]) == (PC_A, None, [])

    APIClient().post(
        "/api/v1/rfid/device/heartbeat/",
        {"SN": "SN-2", "ID": "Reader2"},
        format="json",
        REMOTE_ADDR=PC_A,
    )  # the reader program reports in
    r = client.get("/api/v1/purchases/detected-reader/", REMOTE_ADDR=PC_A).json()
    assert r["reader"]["code"] == "Reader2"
    # A new purchase from this PC uses the detected reader straight away.
    assert new_purchase(shop, PC_A)["reader"] == "Reader2"
    # Another PC detects nothing.
    assert (
        client.get("/api/v1/purchases/detected-reader/", REMOTE_ADDR=PC_B).json()["reader"] is None
    )


def test_two_readers_on_one_address_are_ambiguous(shop):
    tap("Reader1", "SN-1", PC_A)
    tap("Reader2", "SN-2", PC_A)
    r = shop["client"].get("/api/v1/purchases/detected-reader/", REMOTE_ADDR=PC_A).json()
    assert r["reader"] is None and sorted(c["code"] for c in r["candidates"]) == [
        "Reader1",
        "Reader2",
    ]
    assert new_purchase(shop, PC_A)["reader"] is None  # undecided until the first tap


def test_moved_reader_follows_its_new_pc(shop):
    tap("Reader1", "SN-1", PC_A)  # Reader1 is on PC A
    tap("Reader1", "SN-1", PC_B)  # ...now plugged into PC B
    assert RFIDDevice.objects.get(code="Reader1").last_ip == PC_B
    assert new_purchase(shop, PC_B)["reader"] == "Reader1"
    assert new_purchase(shop, PC_A)["reader"] is None


def test_tcp_taps_use_the_connection_address(shop):
    p = new_purchase(shop, PC_A)
    frame = json.dumps({"SN": "SN-1", "ID": "Reader1", "TYPE": "pay", "UID": "04A2B3C4"}).encode()
    assert handle_frame(frame, PC_A)["purchase"] == p["id"]


def test_matching_can_be_switched_off(shop, settings):
    settings.TILL_MATCH_READER_BY_IP = False
    new_purchase(shop, PC_A)
    assert tap("Reader1", "SN-1", PC_A)["purchase"] is None


def test_only_recently_heard_readers_count_as_connected(shop):
    from datetime import timedelta

    from django.utils import timezone

    tap("Reader1", "SN-1", PC_A)
    RFIDDevice.objects.filter(code="Reader1").update(
        last_seen_at=timezone.now() - timedelta(minutes=10)
    )
    r = shop["client"].get("/api/v1/purchases/detected-reader/", REMOTE_ADDR=PC_A).json()
    assert (r["reader"], r["candidates"]) == (None, [])
    assert new_purchase(shop, PC_A)["reader"] is None
