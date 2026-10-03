"""Devices over raw TCP: the devices' text frames ($ID:Door1,TYPE:Input,UID=...$ answered
CARD_OK / CARD_NO / CARD_DENIED) and JSON frames (${"ID":"Door1","Type":"in",...}$)."""

import asyncio
import json

import pytest
from asgiref.sync import async_to_sync

from apps.attendance.models import AttendanceRecord
from apps.attendance.occupancy import occupancy
from apps.developers.models import Developer
from apps.rfid import services as rfid
from apps.rfid.models import Building, CardStatus, RFIDCard, RFIDCardAssignment, RFIDEvent
from apps.rfid.tcp import (
    DoorTCPServer,
    FrameDecoder,
    FrameTooLarge,
    encode,
    handle_frame,
    parse_text_frame,
)

# --- Framing -------------------------------------------------------------------------------


def test_decoder_handles_split_and_batched_frames():
    d = FrameDecoder(max_size=2048)
    assert d.feed(b'${"ID":"Door1",') == []
    assert d.feed(b'"Type":"in"}$') == [b'{"ID":"Door1","Type":"in"}']
    assert d.feed(b"$a$\r\n$b$  $c") == [b"a", b"b"]
    assert d.feed(b"$") == [b"c"]


def test_decoder_ignores_noise_and_empty_frames():
    d = FrameDecoder(max_size=2048)
    assert d.feed(b"garbage\n$$x$") == [b"x"]


def test_decoder_rejects_oversized_frames():
    d = FrameDecoder(max_size=10)
    with pytest.raises(FrameTooLarge):
        d.feed(b"$" + b"x" * 11)


def test_encode_wraps_json_in_dollars():
    assert encode({"a": 1}) == b'${"a":1}$'


# --- Frame handling ------------------------------------------------------------------------

DOOR_IP = "10.20.0.11"


@pytest.fixture
def setup(settings):
    settings.ATTENDANCE_DIRECTION_RULE = "device"
    b1 = Building.objects.create(code="B1", name="Building 1")
    door, _ = rfid.register_device(actor=None, code="Door1", building=b1, allowed_ip=DOOR_IP)
    dev = Developer.objects.create(employee_number="E1", full_name="Ada Lovelace")
    RFIDCardAssignment.objects.create(card=RFIDCard.objects.create(uid="04A2B3C4"), developer=dev)
    return door


def frame(**data):
    return json.dumps(data).encode()


@pytest.mark.django_db
def test_valid_frame_is_recorded(setup):
    reply = handle_frame(frame(ID="Door1", Type="in", UID="04A2B3C4"), DOOR_IP)
    assert reply["result"] == "ACCEPTED" and reply["accepted"] is True
    assert (reply["direction"], reply["message"]) == ("IN", "Welcome, Ada Lovelace")
    assert RFIDEvent.objects.get().device == setup


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("payload", "ip", "error"),
    [
        (b"{not json", DOOR_IP, "Frame is not valid JSON."),
        (frame(Type="in", UID="04A2B3C4"), DOOR_IP, "Missing ID."),
        (
            frame(ID="Door1", Type="in", UID="04A2B3C4"),
            "10.9.9.9",
            "No door device with this ID is registered for this IP.",
        ),
        (
            frame(ID="Door2", Type="in", UID="04A2B3C4"),
            DOOR_IP,
            "No door device with this ID is registered for this IP.",
        ),
    ],
)
def test_rejected_frames(setup, payload, ip, error):
    reply = handle_frame(payload, ip)
    assert (reply["result"], reply["accepted"], reply["error"]) == ("ERROR", False, error)
    assert not RFIDEvent.objects.exists()


@pytest.mark.django_db
def test_invalid_fields_are_reported(setup):
    reply = handle_frame(frame(ID="Door1", Type="pay", UID="04A2B3C4"), DOOR_IP)
    assert reply["result"] == "ERROR" and "in` or `out" in reply["error"]
    reply = handle_frame(frame(ID="Door1", Type="in"), DOOR_IP)
    assert reply["result"] == "ERROR" and "uid" in reply["error"]


@pytest.mark.django_db
def test_till_readers_use_tcp_by_id_from_any_address(setup):
    rfid.register_device(actor=None, code="Reader1", purpose="TILL")
    reply = handle_frame(frame(ID="Reader1", Type="pay", UID="04A2B3C4"), "10.77.0.5")
    assert reply["result"] == "ACCEPTED"
    # A door's ID from the wrong address is not taken for a till.
    reply = handle_frame(frame(ID="Door1", Type="pay", UID="04A2B3C4"), "10.77.0.5")
    assert reply["result"] == "ERROR"


# --- The devices' text frames --------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "fields"),
    [
        (b"ID:Door1,TYPE:Input,UID=DC62B3E3", {"id": "Door1", "type": "Input", "uid": "DC62B3E3"}),
        (
            b"ID:ID:Reader1,TYPE:Pay,UID=DC62B3E3",
            {"id": "Reader1", "type": "Pay", "uid": "DC62B3E3"},
        ),
        (
            b" id : Master1 , type:Master , uid = dc62b3e3 ",
            {"id": "Master1", "type": "Master", "uid": "dc62b3e3"},
        ),
        (b"ID:Door1,junk,UID=1", {"id": "Door1", "uid": "1"}),
    ],
)
def test_parse_text_frame(raw, fields):
    assert parse_text_frame(raw) == fields


def text(door="Door1", kind="Input", uid="04A2B3C4"):
    return f"ID:{door},TYPE:{kind},UID={uid}".encode()


@pytest.mark.django_db
def test_door_text_frames_answer_card_ok_no_denied(setup):
    assert handle_frame(text(kind="Input"), DOOR_IP) == "CARD_OK\r\n"
    assert RFIDEvent.objects.get().direction == "IN"
    assert handle_frame(text(kind="Output"), DOOR_IP) == "CARD_OK\r\n"
    assert AttendanceRecord.objects.order_by("-pk").first().event_type == "OUT"
    assert handle_frame(text(uid="04FFFFFF"), DOOR_IP) == "CARD_NO\r\n"  # unknown card

    RFIDCard.objects.create(uid="04EEEEEE")  # registered, not assigned
    assert handle_frame(text(uid="04EEEEEE"), DOOR_IP) == "CARD_DENIED\r\n"
    RFIDCard.objects.filter(uid="04A2B3C4").update(status=CardStatus.BLOCKED)
    assert handle_frame(text(), DOOR_IP) == "CARD_DENIED\r\n"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("raw", "ip"),
    [
        (text(), "10.9.9.9"),  # wrong address for Door1
        (text(door="Door9"), DOOR_IP),  # unknown ID
        (text(kind="Pay"), DOOR_IP),  # doors don't pay
        (text(uid="ZZ"), DOOR_IP),  # not a UID
        (b"garbage", DOOR_IP),
    ],
)
def test_unusable_text_frames_answer_card_no(setup, raw, ip):
    assert handle_frame(raw, ip) == "CARD_NO\r\n"
    assert not RFIDEvent.objects.exists()


@pytest.mark.django_db
def test_door_units_sharing_an_id_are_separate_devices(setup):
    second, _ = rfid.register_device(
        actor=None, code="Door1", name="Door1-2", building=setup.building, allowed_ip="10.20.0.12"
    )
    assert handle_frame(text(kind="Input"), DOOR_IP) == "CARD_OK\r\n"
    assert handle_frame(text(kind="Output"), "10.20.0.12") == "CARD_OK\r\n"
    assert [e.device for e in RFIDEvent.objects.order_by("pk")] == [setup, second]


# --- End to end over a real socket ---------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_tcp_server_end_to_end(setup):
    # The test client connects from 127.0.0.1, so register that as the door's IP.
    setup.allowed_ip = "127.0.0.1"
    setup.save(update_fields=["allowed_ip"])

    async def scenario():
        server = DoorTCPServer("127.0.0.1", 0)
        await server.start()
        port = server.server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            # Two scans on one kept-open connection; the first split across two writes.
            writer.write(b'${"ID":"Door1","Type":"in",')
            await writer.drain()
            writer.write(b'"UID":"04A2B3C4","client_event_id":"t-1"}$')
            await writer.drain()
            first = await asyncio.wait_for(reader.readuntil(b"}$"), 5)
            writer.write(
                b'\r\n${"ID":"Door1","Type":"out","UID":"04A2B3C4","client_event_id":"t-2",'
                b'"event_time":"2999-01-01T00:00:00Z"}$'
            )
            await writer.drain()
            second = await asyncio.wait_for(reader.readuntil(b"}$"), 5)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.close()
        return first, second

    first, second = async_to_sync(scenario)()
    assert first.startswith(b"$") and first.endswith(b"$")
    reply = json.loads(first[1:-1])
    assert (reply["result"], reply["message"]) == ("ACCEPTED", "Welcome, Ada Lovelace")
    reply2 = json.loads(second[1:-1])
    assert reply2["result"] == "ERROR" and "future" in reply2["error"]

    assert AttendanceRecord.objects.get().event_type == "IN"
    assert occupancy()["total"] == 1


@pytest.mark.django_db(transaction=True)
def test_tcp_server_text_frames_end_to_end(setup):
    setup.allowed_ip = "127.0.0.1"
    setup.save(update_fields=["allowed_ip"])

    async def scenario():
        server = DoorTCPServer("127.0.0.1", 0)
        await server.start()
        port = server.server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"$ID:Door1,TYPE:Input,UID=04A2B3C4$\r\n")
            await writer.drain()
            first = await asyncio.wait_for(reader.readuntil(b"\r\n"), 5)
            writer.write(b"$ID:Door1,TYPE:Output,UID=04FFFFFF$\r\n")
            await writer.drain()
            second = await asyncio.wait_for(reader.readuntil(b"\r\n"), 5)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.close()
        return first, second

    assert async_to_sync(scenario)() == (b"CARD_OK\r\n", b"CARD_NO\r\n")
    assert AttendanceRecord.objects.get().event_type == "IN"


@pytest.mark.django_db(transaction=True)
def test_connection_limit_per_ip(setup, settings):
    settings.RFID_TCP_MAX_CONNECTIONS_PER_IP = 1

    async def scenario():
        server = DoorTCPServer("127.0.0.1", 0)
        await server.start()
        port = server.server.sockets[0].getsockname()[1]
        try:
            r1, w1 = await asyncio.open_connection("127.0.0.1", port)
            await asyncio.sleep(0.1)
            r2, w2 = await asyncio.open_connection("127.0.0.1", port)
            refused = await asyncio.wait_for(r2.read(), 5)
            w1.close()
            await w1.wait_closed()
        finally:
            await server.close()
        return refused

    assert b"Too many connections." in async_to_sync(scenario)()
