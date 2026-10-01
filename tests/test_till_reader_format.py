"""Till readers send {"ID": "Reader1", "TYPE": "pay", "UID": "..."}."""

import uuid
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.accounts.rbac import Roles
from apps.developers.models import Developer
from apps.finance import services as finance
from apps.finance.models import DeveloperAccount
from apps.goods.models import Good
from apps.rfid import services as rfid
from apps.rfid.models import RFIDCard, RFIDCardAssignment, RFIDEvent
from apps.sellers.models import Seller, ServicePosition

pytestmark = pytest.mark.django_db
EVENTS = "/api/v1/rfid/events/"
PIN = "4826"


def device_client(key):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Device {key}")
    return client


@pytest.fixture
def till(make_user):
    seller_user = make_user(Roles.SELLER)
    seller = Seller.objects.create(name="Cafe", user=seller_user)
    counter = ServicePosition.objects.create(seller=seller, name="Counter 1")
    tea = Good.objects.create(
        service_position=counter, name="Tea", price="2.50", kind="SERVICE", track_stock=False
    )
    _, key = rfid.register_device(
        actor=None, code="Reader1", purpose="TILL", service_position=counter
    )
    dev = Developer.objects.create(employee_number="E1", full_name="Ada Lovelace")
    RFIDCardAssignment.objects.create(card=RFIDCard.objects.create(uid="04A2B3C4"), developer=dev)
    account = finance.open_account(dev)
    finance.deposit(actor=None, developer=dev, amount="10.00", idempotency_key="seed-000001")
    finance.set_pin(actor=None, account=account, pin=PIN, current_pin=None)
    seller_client = APIClient()
    seller_client.force_authenticate(seller_user)
    return {
        "reader": device_client(key),
        "seller": seller_client,
        "counter": counter,
        "tea": tea,
        "dev": dev,
    }


def test_till_reader_payload_pays_for_the_open_purchase(till):
    seller = till["seller"]
    pid = seller.post("/api/v1/purchases/", {"service_position": till["counter"].pk}).json()["id"]
    seller.post(f"/api/v1/purchases/{pid}/items/", {"good": till["tea"].pk, "quantity": 2})

    r = till["reader"].post(
        EVENTS, {"ID": "Reader1", "TYPE": "pay", "UID": "04A2B3C4"}, format="json"
    )
    assert r.status_code == 201, r.json()
    body = r.json()
    assert (body["result"], body["purchase"], body["display_message"]) == (
        "ACCEPTED",
        pid,
        "Ada Lovelace - enter PIN",
    )
    assert RFIDEvent.objects.get().direction == ""

    r = seller.post(
        f"/api/v1/purchases/{pid}/confirm/", {"pin": PIN}, HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4())
    )
    assert r.status_code == 201, r.json()
    assert DeveloperAccount.objects.get(developer=till["dev"]).balance == Decimal("5.00")


@pytest.mark.parametrize("value", ["pay", "PAY", "Pay"])
def test_pay_is_case_insensitive(till, value):
    r = till["reader"].post(
        EVENTS, {"id": "reader1", "type": value, "uid": "04a2b3c4"}, format="json"
    )
    assert r.status_code == 201


def test_till_reader_cannot_send_in_or_out(till):
    r = till["reader"].post(
        EVENTS, {"ID": "Reader1", "TYPE": "in", "UID": "04A2B3C4"}, format="json"
    )
    assert r.status_code == 400
    assert r.json()["error"]["details"] == {"type": ["Till readers send `pay`."]}


def test_door_cannot_send_pay():
    _, key = rfid.register_device(actor=None, code="Door1")
    r = device_client(key).post(
        EVENTS, {"ID": "Door1", "Type": "pay", "UID": "04A2B3C4"}, format="json"
    )
    assert r.status_code == 400
    assert r.json()["error"]["details"] == {"type": ["Door devices send `in` or `out`."]}


def test_reader_id_must_match_key(till):
    r = till["reader"].post(
        EVENTS, {"ID": "Reader2", "TYPE": "pay", "UID": "04A2B3C4"}, format="json"
    )
    assert r.status_code == 400
    assert "device_id" in r.json()["error"]["details"]
