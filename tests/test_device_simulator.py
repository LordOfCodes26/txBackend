"""tools/device-simulator talks to the real TCP listener exactly like the devices do."""

import asyncio
import importlib.util
import sys
import threading
from pathlib import Path

import pytest

from apps.developers.models import Developer
from apps.rfid import services as rfid
from apps.rfid.models import Building, RFIDCard, RFIDCardAssignment, RFIDEvent
from apps.rfid.tcp import DoorTCPServer

SIMULATOR = Path(__file__).resolve().parent.parent / "tools/device-simulator/device_simulator.py"
spec = importlib.util.spec_from_file_location("device_simulator", SIMULATOR)
sim = importlib.util.module_from_spec(spec)
sys.modules["device_simulator"] = sim  # dataclasses look the module up while loading
spec.loader.exec_module(sim)


@pytest.fixture
def server():
    """The real listener on a free port, in its own thread (the simulator blocks)."""
    loop = asyncio.new_event_loop()
    listener = DoorTCPServer("127.0.0.1", 0)
    loop.run_until_complete(listener.start())
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield listener.server.sockets[0].getsockname()[1]
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)
    loop.run_until_complete(listener.close())
    loop.close()


@pytest.fixture
def site(db):
    b1 = Building.objects.create(code="B1", name="Building 1")
    # The test PC is 127.0.0.1: register the door unit with that address.
    rfid.register_device(
        actor=None, code="Door1", name="Door1-1", building=b1, allowed_ip="127.0.0.1"
    )
    rfid.register_device(actor=None, code="Reader1", purpose="TILL")
    rfid.register_device(actor=None, code="Master1", purpose="ENROLL")
    dev = Developer.objects.create(employee_number="E1", full_name="Ada")
    RFIDCardAssignment.objects.create(card=RFIDCard.objects.create(uid="DC62B3E3"), developer=dev)


def device(name):
    return next(d for d in sim.preset_devices() if d.name == name)


def test_preset_matches_the_device_list():
    names = [d.name for d in sim.preset_devices()]
    assert names[:4] == ["Master1", "Master2", "Reader1", "Reader2"]
    assert names[4:] == [f"Door1-{n}" for n in range(1, 5)] + [f"Door2-{n}" for n in range(1, 7)]
    assert device("Door1-3").packet("DC62B3E3") == "ID:Door1,TYPE:Output,UID=DC62B3E3"
    assert device("Door2-3").type == "Input" and device("Door2-4").type == "Output"
    assert device("Door2-6").source_ip == "192.168.100.206"
    assert device("Reader1").packet("X") == "ID:Reader1,TYPE:Pay,UID=X"
    assert device("Master2").packet("X") == "ID:Master2,TYPE:Master,UID=X"
    assert sim.clean_uid(" dc:62 b3-e3 ") == "DC62B3E3"


@pytest.mark.django_db(transaction=True)
def test_simulated_taps_get_the_real_answers(server, site):
    door = sim.Device("Door1-1", "Door1", "Input", "127.0.0.1")
    result = sim.send_tap("127.0.0.1", server, door, "DC62B3E3")
    assert (result.reply, result.sent_from, result.error) == ("CARD_OK", "127.0.0.1", "")
    assert RFIDEvent.objects.get().direction == "IN"
    assert sim.send_tap("127.0.0.1", server, door, "04FFFFFF").reply == "CARD_NO"
    RFIDCard.objects.create(uid="04CC0003")  # registered, not assigned
    assert sim.send_tap("127.0.0.1", server, door, "04CC0003").reply == "CARD_DENIED"

    # A till answers CARD_OK only when a purchase waiting for a card takes the tap.
    assert sim.send_tap("127.0.0.1", server, device("Reader1"), "DC62B3E3").reply == "CARD_NO"
    master = device("Master1")
    assert sim.send_tap("127.0.0.1", server, master, "04AA0001").reply == "CARD_NO"  # new
    assert sim.send_tap("127.0.0.1", server, master, "04AA0001").reply == "CARD_OK"  # known


@pytest.mark.django_db(transaction=True)
def test_missing_device_ip_falls_back_or_stops(server, site):
    door = device("Door1-1")  # 192.168.100.151: not an address of the test machine
    result = sim.send_tap("127.0.0.1", server, door, "DC62B3E3")
    assert "doesn't have 192.168.100.151" in result.note
    assert result.reply == "CARD_OK"  # sent from 127.0.0.1, which Door1 is registered with
    stopped = sim.send_tap("127.0.0.1", server, door, "DC62B3E3", fallback=False)
    assert (stopped.reply, stopped.sent_from) == ("", "") and "192.168.100.151" in stopped.error


@pytest.mark.django_db(transaction=True)
def test_command_line_tap(server, site, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("APPDATA", str(tmp_path))  # fresh settings: the preset devices
    argv = ["--tap", "Master1", "04aa0002", "--server", "127.0.0.1", "--port", str(server)]
    code = sim.main(argv)
    out = capsys.readouterr().out
    assert code == 0 and "ID:Master1,TYPE:Master,UID=04AA0002  ->  CARD_NO" in out
    assert sim.main(["--tap", "Nope", "04AA0002"]) == 2
    assert sim.send_tap("127.0.0.1", 1, device("Master1"), "X").error  # nothing listening


def test_settings_are_saved_and_loaded(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    settings = sim.Settings.load()
    assert len(settings.devices) == 14 and settings.server == sim.DEFAULT_SERVER
    settings.devices.append(sim.Device("Door3-1", "Door3", "Input", "192.168.100.31"))
    settings.server = "10.0.0.5"
    for uid in ("A1", "B2", "A1"):
        settings.remember_uid(uid)
    settings.save()
    loaded = sim.Settings.load()
    assert (loaded.server, loaded.uids) == ("10.0.0.5", ["A1", "B2"])
    assert loaded.devices[-1] == sim.Device("Door3-1", "Door3", "Input", "192.168.100.31")
    assert (tmp_path / "DeviceSimulator" / "settings.json").exists()


def test_silent_server_is_reported_as_no_answer(monkeypatch):
    """A server that accepts but never replies: the packet was sent, the reply is missing."""
    import socket

    monkeypatch.setattr(sim, "TIMEOUT_SECONDS", 1)
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(1)
    try:
        result = sim.send_tap("127.0.0.1", silent.getsockname()[1], device("Master1"), "04AA")
    finally:
        silent.close()
    assert result.sent_from == "127.0.0.1"
    assert result.error == "connected and sent, but no answer within 1 s"
