"""The door setup steps in deploy/README-INSTALL.md (section 6), request by request."""

import json

import pytest

from apps.rfid.tcp import handle_frame

pytestmark = pytest.mark.django_db

DOOR_IP = "192.168.1.50"


def tap(uid, kind="in"):
    return handle_frame(json.dumps({"ID": "Door1", "Type": kind, "UID": uid}).encode(), DOOR_IP)


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

    # Step 3: door device, recognised by its code.
    door = api_client.post(
        "/api/v1/rfid/devices/",
        {"code": "Door1", "name": "Building 1 door", "building": b1},
        **json_post,
    )
    assert door.status_code == 201, door.json()
    door_id = door.json()["id"]
    listed = api_client.get("/api/v1/rfid/devices/?purpose=ATTENDANCE").json()["results"]
    assert [d["code"] for d in listed] == ["Door1"]

    # Step 5: before the IP is set, the door is rejected (the log line in the README).
    assert tap("04A2B3C4")["error"] == "No door device with this ID is registered for this IP."

    # Step 6: allow the door's IP; an unregistered card now gets an answer.
    r = api_client.patch(f"/api/v1/rfid/devices/{door_id}/", {"allowed_ip": DOOR_IP}, **json_post)
    assert r.status_code == 200, r.json()
    assert tap("04A2B3C4")["message"] == "Unknown card"
    unknown = api_client.get("/api/v1/rfid/events/?result=UNKNOWN_CARD&ordering=-event_time")
    assert unknown.json()["results"][0]["uid"] == "04A2B3C4"

    # Step 8: developer, card, assignment with PIN.
    dev = api_client.post(
        "/api/v1/developers/",
        {"employee_number": "E001", "full_name": "Ada Kim", "building": b1},
        **json_post,
    ).json()["id"]
    card = api_client.post("/api/v1/rfid/cards/", {"uid": "04A2B3C4"}, **json_post).json()["id"]
    r = api_client.post(
        f"/api/v1/rfid/cards/{card}/assign/",
        {"developer": dev, "pin": "5093", "pin_confirm": "5093"},
        **json_post,
    )
    assert r.status_code == 200, r.json()

    # Step 9: the tap is welcomed and counted.
    assert tap("04A2B3C4")["message"] == "Welcome, Ada Kim"
    occupancy = api_client.get("/api/v1/attendance/occupancy/").json()
    assert occupancy["total"] == 1
    assert [(b["code"], b["count"]) for b in occupancy["buildings"]] == [("B1", 1)]
    events = api_client.get("/api/v1/rfid/events/?ordering=-event_time").json()["results"]
    assert events[0]["result"] == "ACCEPTED"
    assert tap("04A2B3C4", "out")["message"] == "Goodbye, Ada Kim"
