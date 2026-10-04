"""The till and card assign reader steps and answers documented in
deploy/README-OFFLINE-KIT.md (section 11), packet by packet over TCP."""

import pytest

from apps.accounts.rbac import Roles
from apps.developers.models import Developer
from apps.finance import services as finance
from apps.goods.models import Good
from apps.rfid.models import RFIDCard, RFIDCardAssignment, RFIDDevice
from apps.rfid.tcp import handle_frame
from apps.sellers.models import Seller, ServicePosition

pytestmark = pytest.mark.django_db

TILL_PC = "192.168.100.60"  # the seller's PC, where the till reader is plugged in
DESK_PC = "192.168.100.70"  # where the card assign reader is


def tap(uid, reader="Reader1", kind="Pay", ip=TILL_PC):
    """Exactly the packet the reader sends (between the $ signs) and the line it gets back."""
    return handle_frame(f"ID:{reader},TYPE:{kind},UID={uid}".encode(), ip)


def test_readme_till_and_card_assign_readers(auth_client, make_user):
    admin = auth_client(make_user(Roles.ADMIN))

    seller_user = make_user(Roles.SELLER)
    cafe = Seller.objects.create(name="Cafe", user=seller_user)
    shop = Seller.objects.create(name="Shop")

    # Step 1: register the readers (ID only); each till reader belongs to its seller.
    for code, name, purpose, seller in [
        ("Reader1", "Cafe till", "TILL", cafe.pk),
        ("Reader2", "Shop till", "TILL", shop.pk),
        ("Master1", "Front desk", "ENROLL", None),
        ("Master2", "Office", "ENROLL", None),
    ]:
        body = {"code": code, "name": name, "purpose": purpose}
        if seller:
            body["seller"] = seller
        r = admin.post("/api/v1/rfid/devices/", body, format="json")
        assert r.status_code == 201, r.json()
    r = admin.post("/api/v1/rfid/devices/", {"code": "Reader1", "purpose": "TILL"}, format="json")
    assert "code" in r.json()["error"]["details"]  # each ID once

    # A registered card of an active developer with money and a PIN.
    ada = Developer.objects.create(employee_number="E001", full_name="Ada Kim")
    RFIDCardAssignment.objects.create(card=RFIDCard.objects.create(uid="DC62B3E3"), developer=ada)
    finance.deposit(actor=None, developer=ada, amount="50.00", idempotency_key="till-readme-1")
    finance.give_pin(actor=None, account=finance.open_account(ada), pin="5093", action="test")

    # Step 3: the seller opens a purchase on the till page (the Cafe's only reader is used),
    # presses Scan card to buy, the developer taps.
    counter = ServicePosition.objects.create(seller=cafe, name="Counter")
    tea = Good.objects.create(service_position=counter, name="Tea", price="2.50", track_stock=False)
    desk = auth_client(seller_user)
    opened = desk.post("/api/v1/purchases/", {"service_position": counter.pk}, format="json")
    purchase = opened.json()["id"]
    assert opened.json()["reader"] == "Reader1"
    desk.post(
        f"/api/v1/purchases/{purchase}/items/", {"good": tea.pk, "quantity": 2}, format="json"
    )
    assert tap("DC62B3E3") == "CARD_NO\r\n"  # before Scan card to buy: nobody takes it
    desk.post(f"/api/v1/purchases/{purchase}/wait/")
    assert tap("DC62B3E3") == "CARD_OK\r\n"  # $ID:Reader1,TYPE:Pay,UID=DC62B3E3$
    assert RFIDDevice.objects.get(code="Reader1").last_ip == TILL_PC

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

    # What a till answers: CARD_OK only when a purchase waiting for a card took it.
    again = desk.post("/api/v1/purchases/", {"service_position": counter.pk}, format="json")
    desk.post(f"/api/v1/purchases/{again.json()['id']}/wait/")
    bob = Developer.objects.create(employee_number="E002", full_name="Bob", status="SUSPENDED")
    RFIDCardAssignment.objects.create(card=RFIDCard.objects.create(uid="04BB0002"), developer=bob)
    RFIDCard.objects.create(uid="04CC0003")  # registered, not assigned
    blocked = RFIDCard.objects.create(uid="04DD0004", status="BLOCKED")
    cy = Developer.objects.create(employee_number="E003", full_name="Cy")
    RFIDCardAssignment.objects.create(card=blocked, developer=cy)
    RFIDCard.objects.create(uid="04EE0005", status="RETIRED")
    for uid in ("04FFFFFF", "04CC0003", "04DD0004", "04EE0005", "04BB0002"):
        assert tap(uid) == "CARD_NO\r\n", uid
    assert tap("DC62B3E3", reader="Reader9") == "CARD_NO\r\n"  # unknown ID
    assert tap("DC62B3E3", kind="Input") == "CARD_NO\r\n"  # wrong TYPE for a till

    # Step 4 / answers: a card assign reader registers new cards.
    assert tap("04AA0001", reader="Master1", kind="Master", ip=DESK_PC) == "CARD_NO\r\n"
    assert RFIDCard.objects.filter(uid="04AA0001").exists()
    assert tap("04AA0001", reader="Master2", kind="Master", ip=DESK_PC) == "CARD_OK\r\n"
    assert tap("DC62B3E3", reader="Master1", kind="Master", ip=DESK_PC) == "CARD_OK\r\n"
