"""The TCP log: every packet (request) and what was sent back (response), for admins."""

import asyncio
from datetime import timedelta

import pytest
from asgiref.sync import async_to_sync
from django.utils import timezone

from apps.accounts.rbac import Roles
from apps.developers.models import Developer
from apps.rfid import services as rfid
from apps.rfid import tcp
from apps.rfid.models import Building, RFIDCard, RFIDCardAssignment, TCPFrameLog
from apps.rfid.tcp import DoorTCPServer, handle_frame, prune_tcp_log

LOG = "/api/v1/rfid/tcp-log/"
DOOR_IP = "192.168.100.151"


@pytest.fixture
def door(db):
    b1 = Building.objects.create(code="B1", name="Building 1")
    door, _ = rfid.register_device(
        actor=None, code="Door1", name="Door1-1", building=b1, allowed_ip=DOOR_IP
    )
    dev = Developer.objects.create(employee_number="E1", full_name="Ada")
    RFIDCardAssignment.objects.create(card=RFIDCard.objects.create(uid="DC62B3E3"), developer=dev)
    return door


@pytest.mark.django_db
def test_each_packet_is_logged_with_request_and_response(door):
    assert handle_frame(b"ID:Door1,TYPE:Input,UID=DC62B3E3", DOOR_IP) == "CARD_OK\r\n"
    entry = TCPFrameLog.objects.get()
    assert (entry.request, entry.response) == ("ID:Door1,TYPE:Input,UID=DC62B3E3", "CARD_OK\r\n")
    assert (entry.peer_ip, entry.device_code, entry.device, entry.outcome) == (
        DOOR_IP,
        "Door1",
        door,
        "OK",
    )
    assert entry.event.uid == "DC62B3E3" and entry.note == "ACCEPTED"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("frame", "ip", "outcome", "note"),
    [
        (b"ID:Door1,TYPE:Input,UID=DC62B3E3", "10.9.9.9", "REJECTED", "No active device"),
        (b"ID:Door9,TYPE:Input,UID=DC62B3E3", DOOR_IP, "REJECTED", "No active device"),
        (b"ID:Door1,TYPE:Pay,UID=DC62B3E3", DOOR_IP, "INVALID", "type"),
        (b"ID:Door1,TYPE:Input,UID=ZZ", DOOR_IP, "INVALID", "uid"),
    ],
)
def test_refused_packets_say_why(door, frame, ip, outcome, note):
    assert handle_frame(frame, ip) == "CARD_NO\r\n"
    entry = TCPFrameLog.objects.get()
    assert (entry.outcome, entry.response) == (outcome, "CARD_NO\r\n")
    assert entry.request == frame.decode()
    assert note in entry.note


@pytest.mark.django_db
def test_json_packets_log_the_json_response(door):
    reply = handle_frame(b'{"ID": "Door1", "Type": "in", "UID": "DC62B3E3"}', DOOR_IP)
    entry = TCPFrameLog.objects.get()
    assert entry.response.startswith('${"result":"ACCEPTED"') and entry.response.endswith("}$")
    assert reply["result"] == "ACCEPTED" and entry.outcome == "OK"
    handle_frame(b"{not json", DOOR_IP)
    assert TCPFrameLog.objects.latest("id").outcome == "INVALID"


@pytest.mark.django_db
def test_a_server_error_still_answers_and_is_logged(door, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("database gone")

    monkeypatch.setattr(tcp, "_record", broken)
    assert handle_frame(b"ID:Door1,TYPE:Input,UID=DC62B3E3", DOOR_IP) == "CARD_NO\r\n"
    entry = TCPFrameLog.objects.get()
    assert (entry.outcome, entry.response) == ("ERROR", "CARD_NO\r\n")
    assert "database gone" in entry.note


@pytest.mark.django_db
def test_only_admins_read_the_log_with_filters(door, auth_client, make_user):
    handle_frame(b"ID:Door1,TYPE:Input,UID=DC62B3E3", DOOR_IP)
    handle_frame(b"ID:Door9,TYPE:Input,UID=04FFFFFF", DOOR_IP)
    for role in (Roles.BOSS, Roles.MANAGER, Roles.BUILDING_OWNER):
        assert auth_client(make_user(role)).get(LOG).status_code == 403, role
    admin = auth_client(make_user(Roles.ADMIN))
    rows = admin.get(LOG).json()["results"]
    assert [r["device_code"] for r in rows] == ["Door9", "Door1"]  # newest first
    first = rows[1]
    assert (first["request"], first["response"], first["device_name"]) == (
        "ID:Door1,TYPE:Input,UID=DC62B3E3",
        "CARD_OK\r\n",
        "Door1-1",
    )
    assert [r["device_code"] for r in admin.get(f"{LOG}?outcome=REJECTED").json()["results"]] == [
        "Door9"
    ]
    assert len(admin.get(f"{LOG}?text=04FFFFFF").json()["results"]) == 1
    assert len(admin.get(f"{LOG}?text=card_ok").json()["results"]) == 1
    assert len(admin.get(f"{LOG}?peer_ip={DOOR_IP}&device_code=door1").json()["results"]) == 1


@pytest.mark.django_db
def test_old_entries_are_pruned(door, settings):
    settings.RFID_TCP_LOG_DAYS = 30
    handle_frame(b"ID:Door1,TYPE:Input,UID=DC62B3E3", DOOR_IP)
    old = TCPFrameLog.objects.create(outcome="OK", received_at=timezone.now() - timedelta(days=31))
    assert prune_tcp_log() == 1
    assert not TCPFrameLog.objects.filter(pk=old.pk).exists()
    assert TCPFrameLog.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_refused_connections_are_logged(door, settings):
    settings.RFID_TCP_MAX_CONNECTIONS_PER_IP = 1

    async def scenario():
        server = DoorTCPServer("127.0.0.1", 0)
        await server.start()
        port = server.server.sockets[0].getsockname()[1]
        try:
            _r1, w1 = await asyncio.open_connection("127.0.0.1", port)
            await asyncio.sleep(0.1)
            r2, _w2 = await asyncio.open_connection("127.0.0.1", port)
            await asyncio.wait_for(r2.read(), 5)
            w1.close()
            await w1.wait_closed()
        finally:
            await server.close()

    async_to_sync(scenario)()
    entry = TCPFrameLog.objects.get(outcome="REFUSED")
    assert entry.peer_ip == "127.0.0.1" and "Too many connections" in entry.response
