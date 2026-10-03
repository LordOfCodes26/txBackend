"""Card assign readers send $ID:Master1,TYPE:Master,UID=...$ over TCP and are found by ID.

They answer CARD_OK for a card that was already registered and CARD_NO for a new card,
which the tap registers. The card assign page polls /rfid/card-reads/ to fill in the card.
"""

import pytest
from rest_framework.test import APIClient

from apps.accounts.rbac import Roles
from apps.developers.models import Developer
from apps.rfid import services as rfid
from apps.rfid.models import Building, RFIDCard, RFIDCardAssignment, RFIDEvent
from apps.rfid.tcp import handle_frame

pytestmark = pytest.mark.django_db
READS = "/api/v1/rfid/card-reads/"
DEVICES = "/api/v1/rfid/devices/"
NEW_PIN = {"pin": "4826", "pin_confirm": "4826"}


@pytest.fixture
def reader():
    device, _ = rfid.register_device(actor=None, code="Master1", purpose="ENROLL")
    return device


def tap(uid, device_id="Master1", kind="Master", ip="10.0.7.5"):
    return handle_frame(f"ID:{device_id},TYPE:{kind},UID={uid}".encode(), ip)


def staff(make_user, role=Roles.MANAGER, **extra):
    client = APIClient()
    client.force_authenticate(make_user(role, **extra))
    return client


def test_new_card_answers_card_no_and_is_registered(reader):
    assert tap("dc62b3e3") == "CARD_NO\r\n"
    card = RFIDCard.objects.get(uid="DC62B3E3")
    assert (card.status, card.notes) == ("ACTIVE", "Registered by tapping on Master1")
    assert RFIDEvent.objects.get().device == reader
    assert tap("DC62B3E3") == "CARD_OK\r\n"  # now known; not registered twice
    assert RFIDCard.objects.filter(uid="DC62B3E3").count() == 1


def test_found_by_id_from_any_address(reader):
    rfid.register_device(actor=None, code="Master2", purpose="ENROLL")
    assert tap("DC62B3E3", device_id="Master2", ip="192.168.9.9") == "CARD_NO\r\n"
    assert RFIDEvent.objects.get().device.code == "Master2"


@pytest.mark.parametrize(
    ("device_id", "kind"), [("Master9", "Master"), ("Master1", "Pay"), ("Master1", "Input")]
)
def test_unknown_id_or_wrong_type_is_refused(reader, device_id, kind):
    assert tap("DC62B3E3", device_id=device_id, kind=kind) == "CARD_NO\r\n"
    assert not RFIDCard.objects.exists()


def test_till_readers_cannot_send_master(reader):
    rfid.register_device(actor=None, code="Reader1", purpose="TILL")
    assert tap("DC62B3E3", device_id="Reader1") == "CARD_NO\r\n"
    assert not RFIDCard.objects.exists()


def test_card_reads_fill_the_assign_form(reader, make_user):
    client = staff(make_user)
    r = client.get(READS)
    assert r.json()["devices"] == [
        {"id": reader.pk, "code": "Master1", "name": "", "is_online": False}
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
        "notes": "Registered by tapping on Master1",
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


def test_registering_card_assign_readers_needs_unique_ids(make_user):
    client = staff(make_user, Roles.ADMIN)
    r = client.post(DEVICES, {"code": "Master2", "name": "Front desk", "purpose": "ENROLL"})
    assert r.status_code == 201, r.json()
    assert "sn" not in r.json()
    r = client.post(DEVICES, {"code": "master2", "purpose": "ENROLL"})
    assert "code" in r.json()["error"]["details"]
    r = client.post(DEVICES, {"code": "Master2", "purpose": "TILL"})
    assert "code" in r.json()["error"]["details"]
