"""BUILDING_MANAGER: developers, cards, doors, attendance and sales of their own building."""

from decimal import Decimal

import pytest
from asgiref.sync import async_to_sync
from django.db import transaction
from django.utils import timezone

from apps.accounts.rbac import Roles
from apps.attendance.models import AttendanceRecord
from apps.developers.models import Developer
from apps.finance import services as finance
from apps.finance.models import TransactionKind
from apps.purchases.models import Purchase, PurchaseKind
from apps.realtime.consumers import _authorize, _for_buildings
from apps.realtime.tickets import issue_ticket
from apps.rfid import services as rfid
from apps.rfid.models import Building, RFIDCard, RFIDCardAssignment, RFIDEvent
from apps.sellers.models import Seller, ServicePosition

DEVELOPERS = "/api/v1/developers/"
CARDS = "/api/v1/rfid/cards/"
DEVICES = "/api/v1/rfid/devices/"
BUILDINGS = "/api/v1/rfid/buildings/"
EVENTS = "/api/v1/rfid/events/"
RECORDS = "/api/v1/attendance/records/"
OCCUPANCY = "/api/v1/attendance/occupancy/"
PEOPLE = "/api/v1/attendance/occupancy/people/"
PURCHASES = "/api/v1/purchases/"


@pytest.fixture
def site(db, make_user, settings):
    settings.ATTENDANCE_DIRECTION_RULE = "device"
    b1 = Building.objects.create(code="B1", name="Building 1")
    b2 = Building.objects.create(code="B2", name="Building 2")
    bm = make_user(Roles.BUILDING_MANAGER, email="bm1@x.com")
    b1.managers.add(bm)

    ada = Developer.objects.create(employee_number="E1", full_name="Ada", building=b1)
    bob = Developer.objects.create(employee_number="E2", full_name="Bob", building=b2)
    cy = Developer.objects.create(employee_number="E3", full_name="Cy")
    ada_card = RFIDCard.objects.create(uid="04A1")
    bob_card = RFIDCard.objects.create(uid="04B2")
    spare = RFIDCard.objects.create(uid="04C3")
    RFIDCardAssignment.objects.create(card=ada_card, developer=ada)
    RFIDCardAssignment.objects.create(card=bob_card, developer=bob)

    door1, _ = rfid.register_device(actor=None, code="Door1", building=b1)
    door2, _ = rfid.register_device(actor=None, code="Door2", building=b2)
    rfid.register_device(actor=None, code="Reader1", purpose="TILL")
    rfid.record_scan(device=door1, uid="04A1", direction="IN")
    rfid.record_scan(device=door2, uid="04B2", direction="IN")

    seller = Seller.objects.create(name="Cafe")
    p1 = ServicePosition.objects.create(seller=seller, name="Cafe B1", building=b1)
    p2 = ServicePosition.objects.create(seller=seller, name="Cafe B2", building=b2)
    account = finance.open_account(ada)
    finance.deposit(actor=None, developer=ada, amount="200.00", idempotency_key="seed-bm-01")
    for position, total, kind in [
        (p1, "10.00", PurchaseKind.SALE),
        (p1, "20.00", PurchaseKind.BOOKING),
        (p2, "99.00", PurchaseKind.SALE),
    ]:
        with transaction.atomic():
            txn = finance.post_transaction(
                account=account,
                kind=TransactionKind.PURCHASE,
                amount=-Decimal(total),
                actor=None,
                description="test",
                reference="test",
            )
        Purchase.objects.create(
            seller=seller,
            service_position=position,
            kind=kind,
            status="CONFIRMED",
            developer=ada,
            total=Decimal(total),
            account_transaction=txn,
            confirmed_at=timezone.now(),
        )

    class S:
        pass

    s = S()
    s.__dict__.update(locals())
    return s


@pytest.fixture
def client(auth_client, site):
    return auth_client(site.bm)


def results(response):
    body = response.json()
    return body["results"] if isinstance(body, dict) and "results" in body else body


# --- Developers --------------------------------------------------------------------------


def test_developers_of_own_building_only(client, site):
    assert [d["full_name"] for d in results(client.get(DEVELOPERS))] == ["Ada"]
    assert client.get(f"{DEVELOPERS}{site.bob.pk}/").status_code == 404
    assert client.get(f"{DEVELOPERS}{site.cy.pk}/").status_code == 404

    new = {"employee_number": "E9", "full_name": "Dee"}
    assert "building" in client.post(DEVELOPERS, new).json()["error"]["details"]
    r = client.post(DEVELOPERS, new | {"building": site.b2.pk})
    assert "building" in r.json()["error"]["details"]
    r = client.post(DEVELOPERS, new | {"building": site.b1.pk})
    assert (r.status_code, r.json()["building_name"]) == (201, "Building 1")

    r = client.patch(f"{DEVELOPERS}{site.ada.pk}/", {"building": site.b2.pk})
    assert "building" in r.json()["error"]["details"]
    assert client.patch(f"{DEVELOPERS}{site.ada.pk}/", {"phone": "123"}).status_code == 200
    assert client.delete(f"{DEVELOPERS}{site.ada.pk}/").status_code == 403


def test_another_role_with_the_permission_lifts_the_limit(auth_client, site):
    from apps.accounts.models import Role, UserRole

    UserRole.objects.create(user=site.bm, role=Role.objects.get(code=Roles.MANAGER))
    client = auth_client(site.bm)
    assert len(results(client.get(DEVELOPERS))) == 3
    # MANAGER has no purchase.view: sales stay limited to the building.
    assert {p["service_position"] for p in results(client.get(PURCHASES))} == {site.p1.pk}


def test_building_manager_without_a_building_sees_nothing(auth_client, make_user, site):
    client = auth_client(make_user(Roles.BUILDING_MANAGER))
    assert results(client.get(DEVELOPERS)) == []
    assert results(client.get(BUILDINGS)) == []


# --- Cards, doors, buildings, scans ------------------------------------------------------


def test_cards_of_own_developers_and_spare_cards(client, site):
    assert sorted(c["uid"] for c in results(client.get(CARDS))) == ["04A1", "04C3"]
    assert client.post(f"{CARDS}{site.bob_card.pk}/block/", {"reason": "x"}).status_code == 404
    r = client.post(f"{CARDS}{site.spare.pk}/assign/", {"developer": site.bob.pk})
    assert "developer" in r.json()["error"]["details"]
    dee = Developer.objects.create(employee_number="E4", full_name="Dee", building=site.b1)
    assert client.post(f"{CARDS}{site.spare.pk}/assign/", {"developer": dee.pk}).status_code == 200


def test_door_devices_of_own_building(client, site):
    assert [d["code"] for d in results(client.get(DEVICES))] == ["Door1"]
    r = client.post(DEVICES, {"code": "Door3", "name": "x", "building": site.b2.pk})
    assert "building" in r.json()["error"]["details"]
    r = client.post(DEVICES, {"code": "Reader9", "name": "x", "purpose": "TILL"})
    assert "building" in r.json()["error"]["details"]
    r = client.post(DEVICES, {"code": "Door3", "name": "x", "building": site.b1.pk})
    assert r.status_code == 201, r.json()
    r = client.patch(f"{DEVICES}{site.door1.pk}/", {"allowed_ip": "10.0.0.5"})
    assert r.status_code == 200
    assert client.patch(f"{DEVICES}{site.door2.pk}/", {"is_active": False}).status_code == 404


def test_own_building_only(client, site):
    assert [b["code"] for b in results(client.get(BUILDINGS))] == ["B1"]
    assert client.post(BUILDINGS, {"code": "B3", "name": "Building 3"}).status_code == 403
    assert client.delete(f"{BUILDINGS}{site.b1.pk}/").status_code == 403
    r = client.patch(f"{BUILDINGS}{site.b1.pk}/", {"managers": []}, format="json")
    assert "managers" in r.json()["error"]["details"]
    r = client.patch(f"{BUILDINGS}{site.b1.pk}/", {"name": "Main building"})
    assert r.json()["name"] == "Main building"


def test_admin_assigns_building_managers(auth_client, make_user, boss, site):
    other = make_user(Roles.BUILDING_MANAGER)
    r = auth_client(boss).patch(
        f"{BUILDINGS}{site.b2.pk}/", {"managers": [other.pk]}, format="json"
    )
    assert r.json()["managers"] == [other.pk]
    assert [d["full_name"] for d in results(auth_client(other).get(DEVELOPERS))] == ["Bob"]


def test_scans_at_own_doors_only(client, site):
    assert {e["device_code"] for e in results(client.get(EVENTS))} == {"Door1"}
    assert RFIDEvent.objects.count() == 2


# --- Attendance and occupancy ------------------------------------------------------------


def test_attendance_of_own_developers(client, site):
    assert [r["developer"]["full_name"] for r in results(client.get(RECORDS))] == ["Ada"]
    body = {"developer": site.bob.pk, "event_time": timezone.now().isoformat(), "note": "x"}
    assert "developer" in client.post(RECORDS, body).json()["error"]["details"]
    body["developer"] = site.ada.pk
    assert client.post(RECORDS, body).status_code == 201
    bob_record = AttendanceRecord.objects.get(developer=site.bob)
    assert client.post(f"{RECORDS}{bob_record.pk}/void/", {"reason": "x"}).status_code == 404


def test_occupancy_of_own_building(client, site, auth_client, boss):
    body = client.get(OCCUPANCY).json()
    assert [(b["code"], b["count"]) for b in body["buildings"]] == [("B1", 1)]
    assert (body["total"], body["unknown_building"]) == (1, 0)
    assert [p["developer"]["full_name"] for p in results(client.get(PEOPLE))] == ["Ada"]
    assert auth_client(boss).get(OCCUPANCY).json()["total"] == 2


def test_live_occupancy_feed_is_filtered():
    occupancy = {
        "type": "occupancy",
        "data": {
            "total": 3,
            "unknown_building": 1,
            "buildings": [{"id": 1, "count": 1}, {"id": 2, "count": 1}],
        },
    }
    mine = _for_buildings(occupancy, frozenset({1}))
    assert (mine["data"]["total"], mine["data"]["unknown_building"]) == (1, 0)
    scan = {"type": "attendance", "data": {"building": {"id": 2}}}
    assert _for_buildings(scan, frozenset({1})) is None
    assert _for_buildings(scan, frozenset({2})) == scan
    assert _for_buildings(scan, None) == scan


# --- Sales of the building's sell positions ----------------------------------------------


def test_sales_of_own_building_positions(client, site):
    assert {p["service_position"] for p in results(client.get(PURCHASES))} == {site.p1.pk}
    rows = client.get(f"{PURCHASES}performance/").json()
    assert rows == [
        {
            "service_position": site.p1.pk,
            "service_position_name": "Cafe B1",
            "seller": site.seller.pk,
            "seller_name": "Cafe",
            "building": site.b1.pk,
            "building_name": "Building 1",
            "sales_count": 1,
            "sales_total": "10.00",
            "bookings_count": 1,
            "bookings_total": "20.00",
            "total": "30.00",
        }
    ]
    # Read-only: no till.
    assert client.post(PURCHASES, {"service_position": site.p1.pk}).status_code == 403


def test_boss_sees_performance_of_every_position(auth_client, boss, site):
    rows = auth_client(boss).get(f"{PURCHASES}performance/").json()
    assert [(r["service_position_name"], r["total"]) for r in rows] == [
        ("Cafe B2", "99.00"),
        ("Cafe B1", "30.00"),
    ]


@pytest.mark.django_db(transaction=True)
def test_counter_websocket_of_own_building_only(site):
    def allowed(position):
        return async_to_sync(_authorize)(position.pk, issue_ticket(site.bm), "")[0]

    assert allowed(site.p1) is True
    assert allowed(site.p2) is False


# --- Regression: staff scan lists use the caller's language, not DEVICE_LANGUAGE ----------


def test_scan_history_uses_the_callers_language(auth_client, boss, site, settings):
    settings.DEVICE_LANGUAGE = "ko-kp"
    response = auth_client(boss).get(f"{EVENTS}?event_time_after=bad")
    assert response["Content-Language"] == "en"
