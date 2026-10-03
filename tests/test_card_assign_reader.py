"""Card assign readers send {"SN": "...", "ID": "Master", "UID": "..."} (HTTP or $-framed TCP).

The SN identifies the reader. A tapped card is registered if new, and the card assign page
polls /rfid/card-reads/ to fill in the tapped card.
"""

import json

import pytest
from rest_framework.test import APIClient

from apps.accounts.rbac import Roles
from apps.developers.models import Developer
from apps.rfid import services as rfid
from apps.rfid.models import Building, RFIDCard, RFIDCardAssignment, RFIDEvent
from apps.rfid.tcp import handle_frame

pytestmark = pytest.mark.django_db
EVENTS = "/api/v1/rfid/events/"
READS = "/api/v1/rfid/card-reads/"
DEVICES = "/api/v1/rfid/devices/"
SN = "ENR2026000001"
NEW_PIN = {"pin": "4826", "pin_confirm": "4826"}


@pytest.fixture
def reader():
    device, _ = rfid.register_device(actor=None, code="Desk-1", purpose="ENROLL", sn=SN)
    return device


def tap(uid, sn=SN, device_id="Master"):
    body = {"SN": sn, "ID": device_id, "UID": uid}
    return APIClient().post(EVENTS, body, format="json", REMOTE_ADDR="10.0.7.5")


def staff(make_user, role=Roles.MANAGER, **extra):
    client = APIClient()
    client.force_authenticate(make_user(role, **extra))
    return client


def test_tap_registers_a_new_card(reader):
    r = tap("04 aa bb cc")
    assert r.status_code == 201, r.json()
    assert r.json()["display_message"] == "Card read: 04AABBCC"
    card = RFIDCard.objects.get(uid="04AABBCC")
    assert card.status == "ACTIVE"
    assert RFIDEvent.objects.get().device == reader
    tap("04AABBCC")  # a second tap does not register it twice
    assert RFIDCard.objects.filter(uid="04AABBCC").count() == 1


@pytest.mark.parametrize(("sn", "device_id"), [("WRONG", "Master"), (SN, "Desk-1")])
def test_wrong_sn_or_id_is_refused(reader, sn, device_id):
    assert tap("04AABBCC", sn=sn, device_id=device_id).status_code in (401, 403)
    assert not RFIDCard.objects.exists()


def test_master_does_not_open_till_readers(reader):
    rfid.register_device(actor=None, code="Reader1", purpose="TILL", sn="TILL0001")
    assert tap("04AABBCC", sn="TILL0001").status_code in (401, 403)


def test_tap_over_tcp(reader):
    frame = json.dumps({"SN": SN, "ID": "Master", "UID": "04AABBCC"}).encode()
    reply = handle_frame(frame, "10.0.7.5")
    assert reply["message"] == "Card read: 04AABBCC", reply
    assert RFIDCard.objects.filter(uid="04AABBCC").exists()


def test_card_reads_fill_the_assign_form(reader, make_user):
    client = staff(make_user)
    r = client.get(READS)
    assert r.json()["devices"] == [
        {"id": reader.pk, "code": "Desk-1", "name": "", "is_online": False}
    ]
    cursor = client.get(READS, {"device": reader.pk}).json()["cursor"]
    assert client.get(READS, {"device": reader.pk, "after": cursor or 0}).json()["read"] is None

    tap("04AABBCC")
    read = client.get(READS, {"device": reader.pk, "after": cursor or 0}).json()["read"]
    card = RFIDCard.objects.get(uid="04AABBCC")
    assert read["uid"] == "04AABBCC"
    assert read["card"] == {
        "id": card.pk,
        "status": "ACTIVE",
        "label": "",
        "notes": "Registered by tapping on Desk-1",
        "new": True,
        "assigned": False,
        "holder": None,
    }

    b1 = Building.objects.create(code="B1", name="Building 1")
    dev = Developer.objects.create(employee_number="E1", full_name="Ada")
    r = client.post(
        f"/api/v1/rfid/cards/{card.pk}/assign/",
        {"developer": dev.pk, "building": b1.pk, **NEW_PIN},
    )
    assert r.status_code == 200, r.json()
    dev.refresh_from_db()
    assert dev.building == b1
    read = client.get(READS, {"device": reader.pk, "after": cursor or 0}).json()["read"]
    assert (read["card"]["assigned"], read["card"]["holder"]) == (True, "Ada")
    # Nothing new after the last tap.
    last = client.get(READS, {"device": reader.pk}).json()["cursor"]
    assert client.get(READS, {"device": reader.pk, "after": last}).json()["read"] is None


def test_card_reads_need_rfid_assign(reader, make_user):
    assert staff(make_user, Roles.BOSS).get(READS).status_code == 403


def test_building_manager_does_not_see_other_buildings_holders(reader, make_user):
    b1 = Building.objects.create(code="B1", name="Building 1")
    b2 = Building.objects.create(code="B2", name="Building 2")
    bm = make_user(Roles.BUILDING_MANAGER)
    b1.managers.add(bm)
    bob = Developer.objects.create(employee_number="E2", full_name="Bob", building=b2)
    card = RFIDCard.objects.create(uid="04BB0002")
    RFIDCardAssignment.objects.create(card=card, developer=bob)
    tap("04BB0002")
    client = APIClient()
    client.force_authenticate(bm)
    read = client.get(READS, {"device": reader.pk, "after": 0}).json()["read"]
    assert (read["card"]["assigned"], read["card"]["holder"]) == (True, None)
    assert read["card"]["new"] is False  # registered before the tap


def test_registering_a_card_assign_reader(make_user):
    client = staff(make_user, Roles.ADMIN)
    r = client.post(
        DEVICES, {"code": "Desk-2", "name": "Front desk", "purpose": "ENROLL", "sn": "enr 9"}
    )
    assert r.status_code == 201, r.json()
    assert r.json()["sn"] == "ENR 9"
    r = client.post(DEVICES, {"code": "Door9", "purpose": "ATTENDANCE", "sn": "X1"})
    assert "sn" in r.json()["error"]["details"]
