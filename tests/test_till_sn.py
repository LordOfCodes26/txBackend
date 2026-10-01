"""Till readers authenticate by serial number (stored in full) + ID:
{"SN": "...", "ID": "Reader1", "TYPE": "pay", "UID": "..."} (HTTP or $-framed TCP)."""

import json
import uuid
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from rest_framework.test import APIClient

from apps.accounts.rbac import Roles
from apps.audit.models import AuditLog
from apps.developers.models import Developer
from apps.finance import services as finance
from apps.finance.models import DeveloperAccount
from apps.goods.models import Good
from apps.rfid import services as rfid
from apps.rfid.models import RFIDCard, RFIDCardAssignment, RFIDDevice, RFIDEvent
from apps.rfid.tcp import handle_frame
from apps.sellers.models import Seller, ServicePosition

pytestmark = pytest.mark.django_db
EVENTS = "/api/v1/rfid/events/"
SN = "ZK2024A0001234"
PIN = "4826"


@pytest.fixture
def shop(make_user):
    seller_user = make_user(Roles.SELLER)
    seller = Seller.objects.create(name="Cafe", user=seller_user)
    counter = ServicePosition.objects.create(seller=seller, name="Counter 1")
    tea = Good.objects.create(
        service_position=counter, name="Tea", price="2.50", kind="SERVICE", track_stock=False
    )
    reader, _ = rfid.register_device(
        actor=None, code="Reader1", purpose="TILL", service_position=counter, sn=SN
    )
    dev = Developer.objects.create(employee_number="E1", full_name="Ada Lovelace")
    RFIDCardAssignment.objects.create(card=RFIDCard.objects.create(uid="04A2B3C4"), developer=dev)
    account = finance.open_account(dev)
    finance.deposit(actor=None, developer=dev, amount="10.00", idempotency_key="seed-000001")
    finance.set_pin(actor=None, account=account, pin=PIN, current_pin=None)
    seller_client = APIClient()
    seller_client.force_authenticate(seller_user)
    return {"reader": reader, "counter": counter, "tea": tea, "dev": dev, "seller": seller_client}


def tap(sn=SN, reader_id="Reader1", ip="10.0.5.20", **extra):
    body = {"SN": sn, "ID": reader_id, "TYPE": "pay", "UID": "04A2B3C4", **extra}
    return APIClient().post(EVENTS, body, format="json", REMOTE_ADDR=ip)


def test_full_sn_is_stored_and_shown(shop, auth_client, make_user):
    reader = RFIDDevice.objects.get(code="Reader1")
    assert reader.sn == SN
    body = auth_client(make_user(Roles.MANAGER)).get(f"/api/v1/rfid/devices/{reader.pk}/").json()
    assert body["sn"] == SN


def test_sn_is_normalised_and_unique(shop, auth_client, make_user):
    client = auth_client(make_user(Roles.MANAGER))
    r = client.post(
        "/api/v1/rfid/devices/",
        {
            "code": "Reader2",
            "purpose": "TILL",
            "service_position": shop["counter"].pk,
            "sn": f" {SN.lower()} ",
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["details"] == {
        "sn": ["Another device already has this serial number."]
    }
    r = client.post(
        "/api/v1/rfid/devices/",
        {
            "code": "Reader2",
            "purpose": "TILL",
            "service_position": shop["counter"].pk,
            "sn": " zk2024b0009999 ",
        },
    )
    assert r.json()["sn"] == "ZK2024B0009999"
    with pytest.raises(IntegrityError), transaction.atomic():
        RFIDDevice.objects.filter(code="Reader2").update(sn=SN)


def test_till_pays_with_sn_and_no_key(shop):
    seller = shop["seller"]
    pid = seller.post("/api/v1/purchases/", {"service_position": shop["counter"].pk}).json()["id"]
    seller.post(f"/api/v1/purchases/{pid}/items/", {"good": shop["tea"].pk, "quantity": 2})

    r = tap()
    assert r.status_code == 201, r.json()
    assert (r.json()["purchase"], r.json()["display_message"]) == (pid, "Ada Lovelace - enter PIN")
    r = seller.post(
        f"/api/v1/purchases/{pid}/confirm/", {"pin": PIN}, HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4())
    )
    assert r.status_code == 201
    assert DeveloperAccount.objects.get(developer=shop["dev"]).balance == Decimal("5.00")


@pytest.mark.parametrize("value", [SN.lower(), f"  {SN} "])
def test_sn_ignores_case_and_spaces(shop, value):
    assert tap(sn=value).status_code == 201


@pytest.mark.parametrize(
    ("sn", "reader_id"),
    [("WRONG-SN-0000", "Reader1"), (SN, "Reader2"), (SN, "Door1")],
)
def test_wrong_sn_or_id_is_refused(shop, sn, reader_id):
    r = tap(sn=sn, reader_id=reader_id)
    assert r.status_code == 401
    assert not RFIDEvent.objects.exists()


def test_guessing_locks_the_address_out(shop, settings):
    settings.RFID_SN_MAX_FAILURES = 3
    for _ in range(3):
        assert tap(sn="GUESS", ip="10.0.9.9").status_code == 401
    r = tap(ip="10.0.9.9")  # even the right SN is refused now
    assert r.status_code == 401 and "Too many failed attempts" in r.json()["error"]["message"]
    assert tap(ip="10.0.5.20").status_code == 201  # other addresses unaffected


def test_inactive_reader_or_reader_without_sn_is_refused(shop):
    RFIDDevice.objects.filter(code="Reader1").update(is_active=False)
    assert tap().status_code == 401
    RFIDDevice.objects.filter(code="Reader1").update(is_active=True, sn="")
    assert tap().status_code == 401


def test_till_over_tcp_with_sn(shop):
    frame = json.dumps({"SN": SN, "ID": "Reader1", "TYPE": "pay", "UID": "04A2B3C4"}).encode()
    reply = handle_frame(frame, "10.0.5.20")
    assert (reply["result"], reply["message"], reply["purchase"]) == (
        "ACCEPTED",
        "No open purchase at this counter",
        None,
    )
    bad = handle_frame(frame.replace(SN.encode(), b"NOPE"), "10.0.5.20")
    assert (bad["result"], bad["error"]) == ("ERROR", "Unknown till reader ID or serial number.")


def test_heartbeat_with_sn(shop):
    r = APIClient().post(
        "/api/v1/rfid/device/heartbeat/", {"SN": SN, "ID": "Reader1"}, format="json"
    )
    assert (r.status_code, r.json()["code"]) == (200, "Reader1")


def test_managing_the_sn(shop, auth_client, make_user):
    client = auth_client(make_user(Roles.MANAGER))
    reader = RFIDDevice.objects.get(code="Reader1")
    r = client.patch(f"/api/v1/rfid/devices/{reader.pk}/", {"sn": "NEW-SN-9876"})
    assert r.json()["sn"] == "NEW-SN-9876"
    assert tap().status_code == 401 and tap(sn="NEW-SN-9876").status_code == 201
    log = AuditLog.objects.filter(action="rfid.device_updated").latest("id")
    assert (log.old_values, log.new_values) == ({"sn": SN}, {"sn": "NEW-SN-9876"})

    door, _ = rfid.register_device(actor=None, code="Door1")
    r = client.patch(f"/api/v1/rfid/devices/{door.pk}/", {"sn": "X1"})
    assert "sn" in r.json()["error"]["details"]
    with pytest.raises(IntegrityError), transaction.atomic():
        RFIDDevice.objects.filter(pk=door.pk).update(sn="ABC")


def test_key_still_works_for_tills(shop):
    reader, key = RFIDDevice.objects.get(code="Reader1"), None
    key = rfid.rotate_device_key(actor=None, device=reader)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Device {key}")
    r = client.post(EVENTS, {"ID": "Reader1", "TYPE": "pay", "UID": "04A2B3C4"}, format="json")
    assert r.status_code == 201
