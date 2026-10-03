"""The door setup steps in deploy/README-OFFLINE-KIT.md (section 10; README-INSTALL.md
section 8), request by request and packet by packet."""

import pytest

from apps.rfid.models import RFIDCard
from apps.rfid.tcp import handle_frame

pytestmark = pytest.mark.django_db

UNIT_IPS = ["192.168.100.151", "192.168.100.152", "192.168.100.153", "192.168.100.154"]


def tap(uid, kind="Input", ip=UNIT_IPS[0], door="Door1"):
    """Exactly the packet a door unit sends (between the $ signs) and the line it gets back."""
    return handle_frame(f"ID:{door},TYPE:{kind},UID={uid}".encode(), ip)


def test_readme_door_setup(api_client, make_user, settings):
    from apps.accounts.rbac import Roles
    from tests.conftest import PASSWORD

    settings.ATTENDANCE_DIRECTION_RULE = "device"  # as in the installed backend.env
    make_user(Roles.ADMIN, email="admin@chonha.com")
    json_post = {"format": "json"}

    # Step 1: sign in.
    r = api_client.post(
        "/api/v1/auth/token/", {"email": "admin@chonha.com", "password": PASSWORD}, **json_post
    )
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {r.json()['access']}")
    assert api_client.get("/api/v1/auth/me/").status_code == 200

    # Step 2: buildings.
    b1 = api_client.post(
        "/api/v1/rfid/buildings/", {"code": "B1", "name": "Building 1"}, **json_post
    ).json()["id"]

    # Before a unit is registered, it is rejected.
    assert tap("DC62B3E3") == "CARD_NO\r\n"

    # Step 3: every unit of Door1, the same code with its own name and IP.
    for n, ip in enumerate(UNIT_IPS, start=1):
        r = api_client.post(
            "/api/v1/rfid/devices/",
            {"code": "Door1", "name": f"Door1-{n}", "building": b1, "allowed_ip": ip},
            **json_post,
        )
        assert r.status_code == 201, r.json()
    listed = api_client.get("/api/v1/rfid/devices/?purpose=ATTENDANCE").json()["results"]
    assert sorted(d["name"] for d in listed) == ["Door1-1", "Door1-2", "Door1-3", "Door1-4"]
    r = api_client.post(
        "/api/v1/rfid/devices/",
        {"code": "Door1", "name": "twice", "building": b1, "allowed_ip": UNIT_IPS[0]},
        **json_post,
    )
    assert "allowed_ip" in r.json()["error"]["details"]  # one IP per unit

    # Step 5: an unregistered card gets CARD_NO: the connection works. Another IP is refused.
    assert tap("DC62B3E3") == "CARD_NO\r\n"
    unknown = api_client.get("/api/v1/rfid/events/?result=UNKNOWN_CARD&ordering=-event_time")
    assert unknown.json()["results"][0]["uid"] == "DC62B3E3"
    assert tap("DC62B3E3", ip="192.168.100.155") == "CARD_NO\r\n"

    # Step 7: developer, card, assignment with building and PIN.
    dev = api_client.post(
        "/api/v1/developers/", {"employee_number": "E001", "full_name": "Ada Kim"}, **json_post
    ).json()["id"]
    card = api_client.post("/api/v1/rfid/cards/", {"uid": "DC62B3E3"}, **json_post).json()["id"]
    r = api_client.post(
        f"/api/v1/rfid/cards/{card}/assign/",
        {"developer": dev, "building": b1, "pin": "5093", "pin_confirm": "5093"},
        **json_post,
    )
    assert r.status_code == 200, r.json()

    # Step 8: in at unit 1 (Input), counted, out at unit 3 (Output).
    assert tap("DC62B3E3") == "CARD_OK\r\n"
    occupancy = api_client.get("/api/v1/attendance/occupancy/").json()
    assert occupancy["total"] == 1
    assert [(b["code"], b["count"]) for b in occupancy["buildings"]] == [("B1", 1)]
    events = api_client.get("/api/v1/rfid/events/?ordering=-event_time").json()["results"]
    assert events[0]["result"] == "ACCEPTED"
    assert tap("DC62B3E3", kind="Output", ip=UNIT_IPS[2]) == "CARD_OK\r\n"
    assert api_client.get("/api/v1/attendance/occupancy/").json()["total"] == 0

    # What a door answers: CARD_DENIED for a known card that may not enter.
    RFIDCard.objects.create(uid="04CC0003")  # registered, not assigned
    assert tap("04CC0003") == "CARD_DENIED\r\n"
    RFIDCard.objects.filter(uid="DC62B3E3").update(status="BLOCKED")
    assert tap("DC62B3E3", ip=UNIT_IPS[1]) == "CARD_DENIED\r\n"
    assert tap("DC62B3E3", kind="Pay") == "CARD_NO\r\n"  # wrong TYPE for a door
