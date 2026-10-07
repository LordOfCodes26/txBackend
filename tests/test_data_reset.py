"""Data reset: delete all data except users, roles, readers and the stores, after a backup."""

import shutil
import uuid
from pathlib import Path

import pytest
from django.core.management import CommandError, call_command
from django.db import transaction

from apps.accounts.models import Role, User
from apps.accounts.rbac import Roles
from apps.attendance.models import AttendanceRecord
from apps.audit.models import AuditLog
from apps.developers.models import Developer
from apps.finance import services as finance
from apps.goods import services as goods
from apps.goods.models import Good, InventoryMovement
from apps.purchases.models import Purchase
from apps.rfid import services as rfid
from apps.rfid.models import (
    Building,
    RFIDCard,
    RFIDCardAssignment,
    RFIDDevice,
    RFIDEvent,
    TCPFrameLog,
)
from apps.rfid.tcp import handle_frame
from apps.sellers.models import Seller, ServicePosition
from common import data_reset

RESET = "/api/v1/system/data-reset/"
PHRASE = data_reset.CONFIRM_PHRASE
DOOR_IP = "192.168.100.151"


@pytest.fixture
def world(db, make_user, auth_client, settings, tmp_path):
    """A door, a till, a seller with stock, and a developer who came in and bought tea."""
    settings.PURCHASE_ALLOW_MANUAL_CARD_UID = True
    settings.DATA_RESET_BACKUP_DIR = str(tmp_path)
    b1 = Building.objects.create(code="B1", name="Building 1")
    owner = make_user(Roles.BUILDING_OWNER)
    b1.owners.add(owner)
    rfid.register_device(actor=None, code="Door1", name="Door1-1", building=b1, allowed_ip=DOOR_IP)
    seller_user = make_user(Roles.SELLER, username="cafe")
    seller = Seller.objects.create(name="Cafe", user=seller_user)
    position = ServicePosition.objects.create(seller=seller, name="Counter")
    rfid.register_device(
        actor=None, code="Reader1", name="Cafe till", purpose="TILL", seller=seller
    )
    tea = Good.objects.create(service_position=position, name="Tea", price="2.50")
    with transaction.atomic():
        goods.move_stock(good=tea, kind="INITIAL_STOCK", delta=10)

    dev_user = make_user(Roles.DEVELOPER, username="ada")
    dev = Developer.objects.create(employee_number="E1", full_name="Ada", user=dev_user)
    card = RFIDCard.objects.create(uid="04AABBCCDD")
    RFIDCardAssignment.objects.create(card=card, developer=dev)
    account = finance.open_account(dev)
    finance.deposit(actor=None, developer=dev, amount="50.00", idempotency_key="seed-1")
    finance.set_pin(actor=None, account=account, pin="4826", current_pin=None)
    assert handle_frame(b"ID:Door1,TYPE:Input,UID=04AABBCCDD", DOOR_IP) == "CARD_OK\r\n"

    till = auth_client(seller_user)
    purchase = till.post("/api/v1/purchases/", {"service_position": position.pk}).json()
    till.post(f"/api/v1/purchases/{purchase['id']}/items/", {"good": tea.pk, "quantity": 3})
    r = till.post(
        f"/api/v1/purchases/{purchase['id']}/confirm/",
        {"card_uid": "04AABBCCDD", "pin": "4826"},
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert r.status_code == 201, r.json()
    return tea


@pytest.fixture
def admin(auth_client, make_user):
    return auth_client(make_user(Roles.ADMIN, username="root"))


@pytest.fixture
def fake_backup(monkeypatch, tmp_path):
    made = []

    def backup():
        path = tmp_path / f"before-reset-{len(made)}.dump"
        path.write_bytes(b"dump")
        made.append(path)
        return path

    monkeypatch.setattr(data_reset, "backup_database", backup)
    return made


def test_summary_lists_what_goes_and_what_stays(world, admin):
    body = admin.get(RESET).json()
    assert body["phrase"] == PHRASE
    d, k = body["delete"], body["keep"]
    assert d["developers"] == 1 and d["cards"] == 1 and d["scans"] == 1  # the door scan
    assert d["tcp_log"] == 1 and d["attendance_records"] == 1 and d["purchases"] == 1
    assert d["transactions"] == 2 and d["audit_log"] > 0
    assert k["readers"] == 2 and k["sellers"] == 1 and k["goods"] == 1 and k["roles"] == 8


def test_reset_deletes_data_and_keeps_the_setup(world, admin, fake_backup):
    users_before = set(User.objects.values_list("username", flat=True))
    r = admin.post(RESET, {"confirm": PHRASE}, format="json")
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["backup"] == str(fake_backup[0]) and body["deleted"]["developers"] == 1

    for model in (
        Developer,
        RFIDCard,
        RFIDCardAssignment,
        RFIDEvent,
        TCPFrameLog,
        AttendanceRecord,
        Purchase,
    ):
        assert not model._base_manager.exists(), model
    # Kept: users (with roles), readers, buildings and their owners, the store and goods.
    assert set(User.objects.values_list("username", flat=True)) == users_before
    assert User.objects.get(username="ada").roles.filter(code=Roles.DEVELOPER).exists()
    assert Role.objects.count() == 8
    assert set(RFIDDevice.objects.values_list("code", flat=True)) == {"Door1", "Reader1"}
    assert RFIDDevice.objects.get(code="Reader1").seller.name == "Cafe"
    assert Building.objects.get().owners.count() == 1
    assert ServicePosition.objects.count() == 1

    # Stock stays as one opening movement; everything reconciles.
    world.refresh_from_db()
    assert world.quantity == 7
    opening = InventoryMovement.objects.get()
    assert (opening.kind, opening.quantity_delta, opening.quantity_after) == ("INITIAL_STOCK", 7, 7)
    assert finance.ledger_mismatches() == [] and goods.stock_mismatches() == []

    # The audit log starts again with who reset what.
    entry = AuditLog.objects.get()
    assert (entry.action, entry.actor_username) == ("system.data_reset", "root")
    assert entry.new_values["deleted"]["developers"] == 1

    # The system keeps working: the door answers CARD_NO (unknown card now).
    assert handle_frame(b"ID:Door1,TYPE:Input,UID=04AABBCCDD", DOOR_IP) == "CARD_NO\r\n"


def test_wrong_phrase_deletes_nothing(world, admin, fake_backup):
    r = admin.post(RESET, {"confirm": "delete all data"}, format="json")
    assert r.status_code == 400 and r.json()["error"]["code"] == "RESET_NOT_CONFIRMED"
    assert Developer.objects.count() == 1 and fake_backup == []


def test_no_backup_no_reset(world, admin, monkeypatch):
    def broken():
        raise data_reset.BackupFailed(details={"reason": "disk full"})

    monkeypatch.setattr(data_reset, "backup_database", broken)
    r = admin.post(RESET, {"confirm": PHRASE}, format="json")
    assert r.status_code == 500 and r.json()["error"]["code"] == "BACKUP_FAILED"
    assert Developer.objects.count() == 1 and Purchase.objects.count() == 1


@pytest.mark.parametrize(
    "role", [Roles.BOSS, Roles.MANAGER, Roles.FINANCE_MANAGER, Roles.BUILDING_OWNER]
)
def test_only_admins(world, auth_client, make_user, role):
    client = auth_client(make_user(role))
    assert client.get(RESET).status_code == 403
    assert client.post(RESET, {"confirm": PHRASE}, format="json").status_code == 403
    assert Developer.objects.count() == 1


def test_command_needs_the_phrase(world, fake_backup):
    with pytest.raises(CommandError):
        call_command("reset_data", confirm="yes")
    assert Developer.objects.count() == 1
    call_command("reset_data", confirm=PHRASE)
    assert not Developer.objects.exists() and len(fake_backup) == 1
    assert AuditLog.objects.get().actor is None


@pytest.mark.skipif(not shutil.which("pg_dump"), reason="pg_dump not installed")
@pytest.mark.django_db(transaction=True)
def test_real_backup_is_a_pg_dump(settings, tmp_path):
    settings.DATA_RESET_BACKUP_DIR = str(tmp_path / "backups")
    path = data_reset.backup_database()
    assert path.parent == Path(settings.DATA_RESET_BACKUP_DIR)
    assert path.read_bytes()[:5] == b"PGDMP" and not list(path.parent.glob("*.partial"))
