"""Who is inside Building 1 / Building 2 right now, from the door scans."""

from datetime import timedelta

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from channels.testing import WebsocketCommunicator
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.rbac import Roles
from apps.attendance import services as attendance
from apps.attendance.models import AttendanceRecord
from apps.attendance.occupancy import occupancy
from apps.developers.models import Developer
from apps.realtime.tickets import issue_ticket
from apps.rfid import services as rfid
from apps.rfid.models import Building, RFIDCard, RFIDCardAssignment, RFIDDevice
from config.asgi import application

OCC = "/api/v1/attendance/occupancy/"
PEOPLE = "/api/v1/attendance/occupancy/people/"


@pytest.fixture(autouse=True)
def device_rule(settings):
    settings.ATTENDANCE_DIRECTION_RULE = "device"


@pytest.fixture
def site(db):
    b1 = Building.objects.create(code="B1", name="Building 1")
    b2 = Building.objects.create(code="B2", name="Building 2")
    door1, _ = rfid.register_device(actor=None, code="Door1", building=b1)
    door2, _ = rfid.register_device(actor=None, code="Door2", building=b2)
    devs = {}
    for i, (name, dept) in enumerate([("Ada", "Eng"), ("Bob", "Eng"), ("Cy", "QA")], start=1):
        dev = Developer.objects.create(employee_number=f"E{i}", full_name=name, department=dept)
        card = RFIDCard.objects.create(uid=f"04AA00000{i}")
        RFIDCardAssignment.objects.create(card=card, developer=dev)
        devs[name] = (dev, card)
    clock = {"t": timezone.now() - timedelta(minutes=30)}

    def scan(name, door, kind):
        clock["t"] += timedelta(minutes=1)  # strictly increasing, beyond the debounce window
        device = {"Door1": door1, "Door2": door2}[door]
        return rfid.record_scan(
            device=device, uid=devs[name][1].uid, event_time=clock["t"], direction=kind.upper()
        )[0]

    return {"b1": b1, "b2": b2, "devs": devs, "scan": scan}


def counts():
    data = occupancy()
    return {b["code"]: b["count"] for b in data["buildings"]}, data["total"]


@pytest.mark.django_db
def test_counts_per_building_and_total(site):
    scan = site["scan"]
    scan("Ada", "Door1", "in")
    scan("Bob", "Door2", "in")
    scan("Cy", "Door1", "in")
    scan("Cy", "Door1", "out")
    assert counts() == ({"B1": 1, "B2": 1}, 2)


@pytest.mark.django_db
def test_moving_between_buildings(site):
    scan = site["scan"]
    scan("Ada", "Door1", "in")
    assert counts() == ({"B1": 1, "B2": 0}, 1)
    scan("Ada", "Door1", "out")
    assert counts() == ({"B1": 0, "B2": 0}, 0)
    scan("Ada", "Door2", "in")
    assert counts() == ({"B1": 0, "B2": 1}, 1)


@pytest.mark.django_db
def test_forgotten_out_is_never_double_counted(site):
    site["scan"]("Ada", "Door1", "in")
    site["scan"]("Ada", "Door2", "in")  # never scanned out of building 1
    assert counts() == ({"B1": 0, "B2": 1}, 1)


@pytest.mark.django_db
def test_no_scan_out_means_still_inside_on_later_days(site):
    dev, card = site["devs"]["Ada"]
    door1 = RFIDDevice.objects.get(code="Door1")
    rfid.record_scan(
        device=door1, uid=card.uid, direction="IN", event_time=timezone.now() - timedelta(days=3)
    )
    assert counts() == ({"B1": 1, "B2": 0}, 1)


@pytest.mark.django_db
def test_manager_marks_forgotten_scan_out(site, auth_client, make_user):
    dev, card = site["devs"]["Ada"]
    door1 = RFIDDevice.objects.get(code="Door1")
    rfid.record_scan(
        device=door1, uid=card.uid, direction="IN", event_time=timezone.now() - timedelta(days=2)
    )
    client = auth_client(make_user(Roles.MANAGER))
    r = client.post(
        "/api/v1/attendance/records/",
        {
            "developer": dev.pk,
            "event_time": (timezone.now() - timedelta(days=2, hours=-9)).isoformat(),
            "note": "Left without scanning",
            "direction": "OUT",
        },
    )
    assert r.status_code == 201, r.json()
    assert (r.json()["direction"], r.json()["event_type"]) == ("OUT", "OUT")
    assert counts() == ({"B1": 0, "B2": 0}, 0)


@pytest.mark.django_db
def test_terminated_and_deleted_developers_are_not_counted(site):
    site["scan"]("Ada", "Door1", "in")
    site["scan"]("Bob", "Door1", "in")
    Developer.objects.filter(full_name="Ada").update(status="TERMINATED")
    site["devs"]["Bob"][0].soft_delete()
    assert counts() == ({"B1": 0, "B2": 0}, 0)


@pytest.mark.django_db
def test_corrections_change_the_count(site, make_user):
    event = site["scan"]("Ada", "Door1", "in")
    assert counts()[1] == 1
    attendance.void_record(actor=None, record=event.attendance_record, reason="Wrong card")
    assert counts()[1] == 0

    dev = site["devs"]["Bob"][0]
    attendance.add_manual_record(
        actor=None, developer=dev, event_time=timezone.now(), note="Forgot card at the door"
    )
    record = AttendanceRecord.objects.get(developer=dev)
    assert record.event_type == "IN"
    data = occupancy()
    assert (data["total"], data["unknown_building"]) == (1, 1)


# --- API ----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_occupancy_and_people_endpoints(site, auth_client, make_user):
    scan = site["scan"]
    scan("Ada", "Door1", "in")
    scan("Bob", "Door2", "in")
    scan("Cy", "Door2", "in")
    client = auth_client(make_user(Roles.MANAGER))

    body = client.get(OCC).json()
    assert body["total"] == 3
    assert [(b["name"], b["count"]) for b in body["buildings"]] == [
        ("Building 1", 1),
        ("Building 2", 2),
    ]

    people = client.get(PEOPLE).json()["results"]
    assert [(p["developer"]["full_name"], p["building"]["code"]) for p in people] == [
        ("Ada", "B1"),
        ("Bob", "B2"),
        ("Cy", "B2"),
    ]
    assert people[0]["device_code"] == "Door1" and people[0]["since"]

    def names(query):
        return [
            p["developer"]["full_name"] for p in client.get(f"{PEOPLE}?{query}").json()["results"]
        ]

    assert names(f"building={site['b2'].pk}") == ["Bob", "Cy"]
    assert names("department=qa") == ["Cy"]
    assert names("search=bo") == ["Bob"]
    assert names("building=none") == []


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (Roles.BOSS, 200),
        (Roles.MANAGER, 200),
        (Roles.FINANCE_MANAGER, 403),
        (Roles.DEVELOPER, 403),
        (Roles.SELLER, 403),
    ],
)
def test_occupancy_permissions(site, auth_client, make_user, role, expected):
    client = auth_client(make_user(role))
    assert client.get(OCC).status_code == expected
    assert client.get(PEOPLE).status_code == expected


@pytest.mark.django_db
def test_only_attendance_devices_have_buildings(site, auth_client, make_user):
    from apps.sellers.models import Seller, ServicePosition

    client = auth_client(make_user(Roles.MANAGER))
    position = ServicePosition.objects.create(seller=Seller.objects.create(name="S"), name="P")
    r = client.post(
        "/api/v1/rfid/devices/",
        {
            "code": "TILL-9",
            "purpose": "TILL",
            "service_position": position.pk,
            "building": site["b1"].pk,
        },
    )
    assert "building" in r.json()["error"]["details"]
    with pytest.raises(IntegrityError), transaction.atomic():
        RFIDDevice.objects.create(
            code="T",
            purpose="TILL",
            service_position=position,
            building=site["b1"],
            api_key_hash="x",
            api_key_prefix="x",
        )
    assert client.get("/api/v1/rfid/buildings/").json()[0]["name"] == "Building 1"


# --- Live counts ------------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_live_occupancy_socket(site, make_user):
    manager_ticket = issue_ticket(make_user(Roles.MANAGER))
    developer_ticket = issue_ticket(make_user(Roles.DEVELOPER))

    async def scenario():
        comm = WebsocketCommunicator(application, f"/ws/occupancy/?ticket={manager_ticket}")
        assert (await comm.connect())[0]
        snapshot = await comm.receive_json_from(timeout=3)
        assert (snapshot["type"], snapshot["data"]["total"]) == ("occupancy", 0)

        await sync_to_async(site["scan"])("Ada", "Door2", "in")
        announced = await comm.receive_json_from(timeout=3)
        assert announced["type"] == "attendance"
        update = await comm.receive_json_from(timeout=3)
        assert update["type"] == "occupancy"
        assert update["data"]["total"] == 1
        assert {b["code"]: b["count"] for b in update["data"]["buildings"]} == {"B1": 0, "B2": 1}
        await comm.disconnect()

        denied = WebsocketCommunicator(application, f"/ws/occupancy/?ticket={developer_ticket}")
        await denied.connect()
        closed = await denied.receive_output(timeout=3)
        assert (closed["type"], closed["code"]) == ("websocket.close", 4403)

    async_to_sync(scenario)()


@pytest.mark.django_db
def test_building_with_doors_cannot_be_deleted(site, auth_client, make_user):
    client = auth_client(make_user(Roles.MANAGER))
    r = client.delete(f"/api/v1/rfid/buildings/{site['b1'].pk}/")
    assert (r.status_code, r.json()["error"]["code"]) == (409, "IN_USE")
    empty = Building.objects.create(code="B9", name="Annex")
    assert client.delete(f"/api/v1/rfid/buildings/{empty.pk}/").status_code == 204


@pytest.mark.django_db(transaction=True)
def test_live_feed_announces_each_door_scan(site, make_user):
    ticket = issue_ticket(make_user(Roles.MANAGER))

    async def scenario():
        comm = WebsocketCommunicator(application, f"/ws/occupancy/?ticket={ticket}")
        assert (await comm.connect())[0]
        await comm.receive_json_from(timeout=3)  # snapshot

        await sync_to_async(site["scan"])("Ada", "Door1", "in")
        scan = await comm.receive_json_from(timeout=3)
        assert scan["type"] == "attendance"
        d = scan["data"]
        assert (d["developer"]["full_name"], d["direction"], d["result"]) == (
            "Ada",
            "IN",
            "ACCEPTED",
        )
        assert (d["device_code"], d["building"]["code"], d["display_message"]) == (
            "Door1",
            "B1",
            "Welcome, Ada",
        )
        counts = await comm.receive_json_from(timeout=3)
        assert (counts["type"], counts["data"]["total"]) == ("occupancy", 1)

        # Rejected cards are announced too (no occupancy change follows).
        door1 = await sync_to_async(RFIDDevice.objects.get)(code="Door1")
        await sync_to_async(rfid.record_scan)(device=door1, uid="04FFFFFFFF", direction="IN")
        rejected = await comm.receive_json_from(timeout=3)
        assert (rejected["data"]["result"], rejected["data"]["accepted"]) == ("UNKNOWN_CARD", False)
        assert await comm.receive_nothing(timeout=0.5)
        await comm.disconnect()

    async_to_sync(scenario)()
