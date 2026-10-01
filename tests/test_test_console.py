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
    r = auth_client(make_user(Roles.ADMIN)).post(
        SCAN, {"door": "Door1", "type": "in", "uid": "04A2B3C4"}
    )
    assert r.status_code == 404


# --- Simulated till taps for frontend development ---------------------------------------

import uuid  # noqa: E402
from decimal import Decimal  # noqa: E402

from rest_framework.test import APIClient  # noqa: E402

from apps.finance import services as finance  # noqa: E402
from apps.finance.models import DeveloperAccount  # noqa: E402
from apps.goods.models import Good  # noqa: E402
from apps.purchases.models import Purchase  # noqa: E402
from apps.rfid.models import RFIDDevice  # noqa: E402
from apps.sellers.models import Seller, ServicePosition  # noqa: E402

TAP = "/api/v1/test-console/simulate-tap/"


@pytest.fixture
def till(settings, make_user):
    settings.TEST_CONSOLE_ENABLED = True
    seller_user = make_user(Roles.SELLER, email="cafe@x.com")
    seller = Seller.objects.create(name="Cafe", user=seller_user)
    counter = ServicePosition.objects.create(seller=seller, name="Counter 1")
    tea = Good.objects.create(
        service_position=counter, name="Tea", price="2.50", kind="SERVICE", track_stock=False
    )
    dev = Developer.objects.create(employee_number="E1", full_name="Ada Lovelace")
    RFIDCardAssignment.objects.create(card=RFIDCard.objects.create(uid="04A2B3C4"), developer=dev)
    acc = finance.open_account(dev)
    finance.deposit(actor=None, developer=dev, amount="10.00", idempotency_key="seed-000001")
    finance.set_pin(actor=None, account=acc, pin="4826", current_pin=None)
    client = APIClient()
    client.force_authenticate(seller_user)
    return {
        "client": client,
        "seller_user": seller_user,
        "counter": counter,
        "tea": tea,
        "dev": dev,
    }


def new_purchase(till, **extra):
    c = till["client"]
    p = c.post(
        "/api/v1/purchases/",
        {"service_position": till["counter"].pk, **extra},
        REMOTE_ADDR="10.9.9.9",
    ).json()
    c.post(f"/api/v1/purchases/{p['id']}/items/", {"good": till["tea"].pk, "quantity": 2})
    return p["id"]


def test_simulated_tap_pays_a_purchase_end_to_end(till):
    pid = new_purchase(till)
    r = till["client"].post(TAP, {"purchase": pid, "developer": till["dev"].pk})
    assert r.status_code == 201, r.json()
    assert (r.json()["result"], r.json()["purchase"]) == ("ACCEPTED", pid)
    assert r.json()["display_message"] == "Ada Lovelace - enter PIN"
    purchase = Purchase.objects.get(pk=pid)
    assert purchase.reader.code == f"SIM-{till['seller_user'].pk}"

    detail = till["client"].get(f"/api/v1/purchases/{pid}/").json()
    assert detail["presented_card"]["developer"]["full_name"] == "Ada Lovelace"
    r = till["client"].post(
        f"/api/v1/purchases/{pid}/confirm/", {"pin": "4826"}, HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4())
    )
    assert r.status_code == 201
    assert DeveloperAccount.objects.get(developer=till["dev"]).balance == Decimal("5.00")


def test_simulated_tap_with_unknown_or_blocked_card(till):
    pid = new_purchase(till)
    r = till["client"].post(TAP, {"purchase": pid, "uid": "04:ff:ff:ff"})
    assert (r.json()["result"], r.json()["accepted"]) == ("UNKNOWN_CARD", False)
    RFIDCard.objects.filter(uid="04A2B3C4").update(status="BLOCKED")
    r = till["client"].post(TAP, {"purchase": pid, "developer": till["dev"].pk})
    assert r.json()["result"] == "BLOCKED_CARD"


def test_testers_get_separate_simulated_readers(till, make_user):
    other_user = make_user(Roles.ADMIN)  # purchase.* staff, another tester
    other = APIClient()
    other.force_authenticate(other_user)
    mine = new_purchase(till)
    theirs = other.post(
        "/api/v1/purchases/", {"service_position": till["counter"].pk}, REMOTE_ADDR="10.9.9.8"
    ).json()["id"]
    other.post(f"/api/v1/purchases/{theirs}/items/", {"good": till["tea"].pk})
    till["client"].post(TAP, {"purchase": mine, "developer": till["dev"].pk})
    r = other.post(TAP, {"purchase": theirs, "developer": till["dev"].pk})
    assert r.json()["purchase"] == theirs
    assert Purchase.objects.get(pk=mine).presented_event.device.code != (
        Purchase.objects.get(pk=theirs).presented_event.device.code
    )


def test_explicit_reader_is_kept(till):
    rfid.register_device(actor=None, code="Reader1", purpose="TILL")
    pid = new_purchase(till, reader="Reader1")
    till["client"].post(TAP, {"purchase": pid, "developer": till["dev"].pk})
    assert Purchase.objects.get(pk=pid).reader.code == "Reader1"
    assert not RFIDDevice.objects.filter(code__startswith="SIM-").exists()


def test_simulated_tap_rules(till, make_user, settings):
    pid = new_purchase(till)
    stranger = APIClient()
    stranger_user = make_user(Roles.SELLER)
    Seller.objects.create(name="Other", user=stranger_user)
    stranger.force_authenticate(stranger_user)
    assert stranger.post(TAP, {"purchase": pid, "developer": till["dev"].pk}).status_code == 404

    nobody = Developer.objects.create(employee_number="E9", full_name="No Card")
    r = till["client"].post(TAP, {"purchase": pid, "developer": nobody.pk})
    assert "developer" in r.json()["error"]["details"]

    till["client"].post(f"/api/v1/purchases/{pid}/cancel/")
    assert (
        till["client"].post(TAP, {"purchase": pid, "developer": till["dev"].pk}).status_code == 400
    )

    settings.TEST_CONSOLE_ENABLED = False
    assert (
        till["client"]
        .post(TAP, {"purchase": new_purchase(till), "developer": till["dev"].pk})
        .status_code
        == 404
    )


def test_test_cards_list(till, make_user):
    rows = till["client"].get("/api/v1/test-console/cards/?search=ada").json()
    assert rows == [
        {
            "developer": till["dev"].pk,
            "employee_number": "E1",
            "full_name": "Ada Lovelace",
            "developer_status": "ACTIVE",
            "card_uid": "04A2B3C4",
            "card_status": "ACTIVE",
            "balance": "10.00",
            "account_status": "ACTIVE",
            "has_pin": True,
        }
    ]
    dev_client = APIClient()
    dev_client.force_authenticate(make_user(Roles.DEVELOPER))
    assert dev_client.get("/api/v1/test-console/cards/").status_code == 404
