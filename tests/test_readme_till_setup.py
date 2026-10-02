"""The till reader steps and replies documented in deploy/README-OFFLINE-KIT.md (section
"Connecting the till readers"), request by request. Till programs use HTTPS only."""

import json
import os

import pytest

from apps.accounts.rbac import Roles
from apps.developers.models import Developer
from apps.finance import services as finance
from apps.goods.models import Good
from apps.rfid.models import RFIDCard, RFIDCardAssignment
from apps.sellers.models import Seller, ServicePosition

pytestmark = pytest.mark.django_db

SN = "ZK2024A0001234"
TILL_PC = "192.168.1.60"  # the seller's PC, where the till program runs
SHOW = os.environ.get("SHOW_REPLIES")  # SHOW_REPLIES=1 pytest -s: print the replies


def show(title, body):
    if SHOW:
        print(f"\n--- {title}\n{json.dumps(body, indent=2, ensure_ascii=False)}")


def tap(till, uid, sn=SN):
    return till.post(
        "/api/v1/rfid/events/",
        {"SN": sn, "ID": "Reader1", "TYPE": "pay", "UID": uid},
        format="json",
        REMOTE_ADDR=TILL_PC,
    )


def test_readme_till_setup(auth_client, make_user):
    from rest_framework.test import APIClient

    admin = auth_client(make_user(Roles.ADMIN))

    # Step 2: register the reader with its serial number.
    r = admin.post(
        "/api/v1/rfid/devices/",
        {"code": "Reader1", "name": "Cafe till", "purpose": "TILL", "sn": SN},
        format="json",
    )
    assert r.status_code == 201, r.json()

    # Step 4: heartbeat from the till program (no key: SN + ID).
    till = APIClient()  # the till program: no user, identified by SN + ID
    r = till.post(
        "/api/v1/rfid/device/heartbeat/",
        {"SN": SN, "ID": "Reader1", "app_version": "till-agent 1.0"},
        format="json",
        REMOTE_ADDR=TILL_PC,
    )
    assert r.status_code == 200, r.json()
    show("heartbeat reply", r.json())
    device = admin.get("/api/v1/rfid/devices/?purpose=TILL").json()["results"][0]
    assert (device["online"], device["last_ip"]) == (True, TILL_PC)

    # A registered card of an active developer with money and a PIN.
    ada = Developer.objects.create(employee_number="E001", full_name="Ada Kim")
    RFIDCardAssignment.objects.create(card=RFIDCard.objects.create(uid="04A2B3C4"), developer=ada)
    finance.deposit(actor=None, developer=ada, amount="50.00", idempotency_key="till-readme-1")
    finance.give_pin(actor=None, account=finance.open_account(ada), pin="5093", action="test")

    # Step 5: a tap with no purchase open.
    r = till.post(
        "/api/v1/rfid/events/",
        {"SN": SN, "ID": "Reader1", "TYPE": "pay", "UID": "04A2B3C4"},
        format="json",
        REMOTE_ADDR=TILL_PC,
    )
    assert r.status_code == 201, r.json()
    body = r.json()
    show("tap, no purchase open (HTTP)", body)
    assert (body["result"], body["accepted"], body["purchase"]) == ("ACCEPTED", True, None)
    assert body["display_message"] == "No open purchase for this reader"

    # Step 6: the seller opens a purchase on the till page, the developer taps.
    seller_user = make_user(Roles.SELLER)
    cafe = Seller.objects.create(name="Cafe", user=seller_user)
    counter = ServicePosition.objects.create(seller=cafe, name="Counter")
    tea = Good.objects.create(service_position=counter, name="Tea", price="2.50", track_stock=False)
    desk = auth_client(seller_user)
    purchase = desk.post(
        "/api/v1/purchases/", {"service_position": counter.pk, "reader": "Reader1"}, format="json"
    ).json()["id"]
    desk.post(
        f"/api/v1/purchases/{purchase}/items/", {"good": tea.pk, "quantity": 2}, format="json"
    )

    r = tap(till, "04A2B3C4")
    assert r.status_code == 201, r.json()
    body = r.json()
    show("registered card, purchase open", body)
    assert (body["result"], body["accepted"], body["display_message"], body["purchase"]) == (
        "ACCEPTED",
        True,
        "Ada Kim - enter PIN",
        purchase,
    )
    assert body["developer"]["full_name"] == "Ada Kim"

    # The seller's screen shows who tapped; the developer types the PIN; paid.
    presented = desk.get(f"/api/v1/purchases/{purchase}/").json()["presented_card"]
    assert presented["developer"]["full_name"] == "Ada Kim"
    r = desk.post(
        f"/api/v1/purchases/{purchase}/confirm/",
        {"pin": "5093"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="till-readme-confirm-1",
    )
    assert r.status_code == 201, r.json()
    assert (r.json()["status"], r.json()["total"], r.json()["balance_after"]) == (
        "CONFIRMED",
        "5.00",
        "45.00",
    )

    # Step 7: every other kind of card (a purchase is open again).
    desk.post(
        "/api/v1/purchases/", {"service_position": counter.pk, "reader": "Reader1"}, format="json"
    )
    bob = Developer.objects.create(employee_number="E002", full_name="Bob", status="SUSPENDED")
    RFIDCardAssignment.objects.create(card=RFIDCard.objects.create(uid="04BB0002"), developer=bob)
    RFIDCard.objects.create(uid="04CC0003")  # registered, not assigned
    blocked = RFIDCard.objects.create(uid="04DD0004", status="BLOCKED")
    cy = Developer.objects.create(employee_number="E003", full_name="Cy")
    RFIDCardAssignment.objects.create(card=blocked, developer=cy)
    RFIDCard.objects.create(uid="04EE0005", status="RETIRED")
    expected = {
        "04FFFFFF": ("UNKNOWN_CARD", "Unknown card"),
        "04CC0003": ("UNASSIGNED_CARD", "Card not assigned"),
        "04DD0004": ("BLOCKED_CARD", "Card blocked"),
        "04EE0005": ("RETIRED_CARD", "Card no longer valid"),
        "04BB0002": ("INACTIVE_DEVELOPER", "Not active - contact your manager"),
    }
    for uid, (result, message) in expected.items():
        r = tap(till, uid)
        body = r.json()
        show(result, body)
        assert r.status_code == 201
        assert (body["result"], body["accepted"], body["display_message"], body["purchase"]) == (
            result,
            False,
            message,
            None,
        )

    # Step 8: wrong serial number.
    r = tap(till, "04A2B3C4", sn="WRONG-SN")
    show("wrong SN", {"status": r.status_code, **r.json()})
    assert r.status_code == 401
    assert r.json() == {
        "error": {
            "code": "AUTHENTICATION_FAILED",
            "message": "Unknown till reader ID or serial number.",
        }
    }
