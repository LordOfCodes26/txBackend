"""Till readers are registered on their own, then assigned to a seller (or unassigned)."""

import pytest

from apps.accounts.rbac import Roles
from apps.audit.models import AuditLog
from apps.purchases.models import Purchase
from apps.rfid.models import Building, RFIDDevice
from apps.sellers.models import Seller, ServicePosition

DEVICES = "/api/v1/rfid/devices/"


@pytest.fixture
def admin(auth_client, make_user):
    return auth_client(make_user(Roles.ADMIN, username="root"))


@pytest.fixture
def stores(db):
    cafe = Seller.objects.create(name="Cafe")
    shop = Seller.objects.create(name="Shop")
    return cafe, shop


def assign(client, device_id, seller):
    return client.post(f"{DEVICES}{device_id}/assign-seller/", {"seller": seller}, format="json")


def test_register_without_seller_then_assign_move_unassign(admin, stores):
    cafe, shop = stores
    r = admin.post(DEVICES, {"code": "Reader7", "name": "Spare", "purpose": "TILL"}, format="json")
    assert r.status_code == 201, r.json()
    device = RFIDDevice.objects.get(code="Reader7")
    assert device.seller is None

    assert assign(admin, device.pk, cafe.pk).json()["seller"] == cafe.pk
    counter = ServicePosition.objects.create(seller=cafe, name="Counter")
    draft = Purchase.objects.create(seller=cafe, service_position=counter, reader=device)
    assert assign(admin, device.pk, shop.pk).json()["seller_name"] == "Shop"
    draft.refresh_from_db()
    assert draft.reader is None  # the old store's unfinished sale lets go of it
    assert assign(admin, device.pk, None).json()["seller"] is None
    actions = list(
        AuditLog.objects.filter(entity_id=str(device.pk)).values_list("action", flat=True)
    )
    assert actions.count("rfid.reader_assigned") == 2 and "rfid.reader_unassigned" in actions


def test_only_till_readers(admin, stores):
    b1 = Building.objects.create(code="B1", name="Building 1")
    door = RFIDDevice.objects.create(
        code="Door1", name="Door1-1", building=b1, allowed_ip="10.0.0.5"
    )
    r = assign(admin, door.pk, stores[0].pk)
    assert r.status_code == 400 and r.json()["error"]["code"] == "NOT_A_TILL_READER"


def test_needs_reader_management(auth_client, make_user, stores):
    device = RFIDDevice.objects.create(code="Reader8", name="x", purpose="TILL")
    client = auth_client(make_user(Roles.FINANCE_MANAGER))
    assert assign(client, device.pk, stores[0].pk).status_code == 403


def test_list_unassigned_till_readers(admin, stores):
    RFIDDevice.objects.create(code="Reader1", name="a", purpose="TILL", seller=stores[0])
    RFIDDevice.objects.create(code="Reader2", name="b", purpose="TILL")
    r = admin.get(f"{DEVICES}?purpose=TILL&assigned=false")
    assert [d["code"] for d in r.json()["results"]] == ["Reader2"]
    r = admin.get(f"{DEVICES}?purpose=TILL&assigned=true")
    assert [d["code"] for d in r.json()["results"]] == ["Reader1"]
