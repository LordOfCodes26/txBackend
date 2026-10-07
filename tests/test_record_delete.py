"""Deleting records for good, with their history (Admin): developer, card, reader, seller,
service position, good, user. A backup first; ledgers, stock and attendance stay right."""

import uuid
from datetime import timedelta

import pytest
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.rbac import Roles
from apps.attendance.models import AttendanceRecord, DailyAttendance, DeveloperPresence
from apps.audit.models import AuditLog
from apps.developers.models import Developer
from apps.finance import services as finance
from apps.finance.models import AccountTransaction, DeveloperAccount
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

URL = "/api/v1/system/records/{}/{}/"
DOOR_IN, DOOR_OUT = "192.168.100.151", "192.168.100.153"


def books_balanced():
    assert finance.ledger_mismatches() == []
    assert goods.stock_mismatches() == []


@pytest.fixture(autouse=True)
def no_real_backup(monkeypatch, tmp_path):
    made = []

    def backup(prefix="before-reset"):
        path = tmp_path / f"{prefix}.dump"
        path.write_bytes(b"PGDMP")
        made.append(path)
        return path

    monkeypatch.setattr(data_reset, "backup_database", backup)
    return made


@pytest.fixture
def world(db, make_user, auth_client, settings):
    """Ada and Bob, each with a card, who went in and out; Ada bought tea twice."""
    settings.PURCHASE_ALLOW_MANUAL_CARD_UID = True
    settings.ATTENDANCE_DIRECTION_RULE = "device"
    b1 = Building.objects.create(code="B1", name="Building 1")
    door_in, _ = rfid.register_device(
        actor=None, code="Door1", name="Door1-1", building=b1, allowed_ip=DOOR_IN, direction="IN"
    )
    door_out, _ = rfid.register_device(
        actor=None, code="Door1", name="Door1-3", building=b1, allowed_ip=DOOR_OUT, direction="OUT"
    )
    seller_user = make_user(Roles.SELLER, username="cafe")
    cafe = Seller.objects.create(name="Cafe", user=seller_user)
    counter = ServicePosition.objects.create(seller=cafe, name="Counter", building=b1)
    till, _ = rfid.register_device(
        actor=None, code="Reader1", name="Cafe till", purpose="TILL", seller=cafe
    )
    tea = Good.objects.create(service_position=counter, name="Tea", price="2.50")
    cake = Good.objects.create(service_position=counter, name="Cake", price="4.00")
    with transaction.atomic():
        goods.move_stock(good=tea, kind="INITIAL_STOCK", delta=20)
        goods.move_stock(good=cake, kind="INITIAL_STOCK", delta=5)

    people = {}
    for number, (name, uid) in enumerate([("Ada", "04AA0001"), ("Bob", "04BB0002")], start=1):
        login = make_user(Roles.DEVELOPER, username=name.lower())
        dev = Developer.objects.create(employee_number=f"E{number}", full_name=name, user=login)
        card = RFIDCard.objects.create(uid=uid)
        RFIDCardAssignment.objects.create(card=card, developer=dev)
        account = finance.open_account(dev)
        finance.deposit(actor=None, developer=dev, amount="50.00", idempotency_key=f"seed-{uid}")
        finance.set_pin(actor=None, account=account, pin="4826", current_pin=None)
        assert handle_frame(f"ID:Door1,TYPE:Input,UID={uid}".encode(), DOOR_IN) == "CARD_OK\r\n"
        people[name] = (dev, card)

    client = auth_client(seller_user)
    for items in ([(tea, 2)], [(tea, 1), (cake, 1)]):
        purchase = client.post("/api/v1/purchases/", {"service_position": counter.pk}).json()
        for good, qty in items:
            client.post(
                f"/api/v1/purchases/{purchase['id']}/items/", {"good": good.pk, "quantity": qty}
            )
        r = client.post(
            f"/api/v1/purchases/{purchase['id']}/confirm/",
            {"card_uid": "04AA0001", "pin": "4826"},
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        assert r.status_code == 201, r.json()
    books_balanced()

    class W:
        pass

    w = W()
    w.__dict__.update(
        b1=b1,
        door_in=door_in,
        door_out=door_out,
        cafe=cafe,
        counter=counter,
        till=till,
        tea=tea,
        cake=cake,
        ada=people["Ada"][0],
        ada_card=people["Ada"][1],
        bob=people["Bob"][0],
        bob_card=people["Bob"][1],
        seller_user=seller_user,
    )
    return w


@pytest.fixture
def admin(auth_client, make_user):
    return auth_client(make_user(Roles.ADMIN, username="root"))


def delete(client, kind, pk, confirm="DELETE"):
    return client.post(URL.format(kind, pk), {"confirm": confirm}, format="json")


def test_preview_then_delete_a_developer_with_history(world, admin, no_real_backup):
    preview = admin.get(URL.format("developer", world.ada.pk)).json()
    assert preview["label"] == "E1 Ada" and preview["phrase"] == "DELETE"
    d = preview["deletes"]
    assert (d["scans"], d["attendance_records"], d["purchases"]) == (1, 1, 2)  # 1 door scan
    assert d["transactions"] == 3 and d["card_assignments"] == 1  # deposit + 2 purchases
    assert preview["keeps"] == {"login": 1}

    r = delete(admin, "developer", world.ada.pk)
    assert r.status_code == 200, r.json()
    assert r.json()["backup"].endswith("before-delete-developer.dump") and no_real_backup
    assert not Developer.all_objects.filter(pk=world.ada.pk).exists()
    assert not RFIDEvent.objects.filter(developer_id=world.ada.pk).exists()
    assert not AttendanceRecord.objects.filter(developer_id=world.ada.pk).exists()
    assert not DeveloperAccount.objects.filter(developer_id=world.ada.pk).exists()
    assert not Purchase.objects.exists()
    # Kept: her login, her card (unassigned), Bob, stock as it is (sold is sold).
    assert User.objects.filter(username="ada").exists()
    card = RFIDCard.objects.get(pk=world.ada_card.pk)
    assert not card.assignments.exists()
    assert Developer.objects.filter(pk=world.bob.pk).exists()
    world.tea.refresh_from_db()
    assert world.tea.quantity == 17
    books_balanced()
    log = AuditLog.objects.get(action="system.developer_deleted")
    assert log.old_values == {"record": "E1 Ada"} and log.new_values["deleted"]["purchases"] == 2


def test_delete_a_card_recomputes_attendance_and_keeps_sales(world, admin):
    out = timezone.now() + timedelta(minutes=1)
    rfid.record_scan(device=world.door_out, uid="04AA0001", event_time=out, direction="OUT")
    assert DailyAttendance.objects.get(developer=world.ada).record_count == 2
    r = delete(admin, "card", world.ada_card.pk)
    assert r.status_code == 200, r.json()
    assert not RFIDCard.objects.filter(pk=world.ada_card.pk).exists()
    assert not RFIDEvent.objects.filter(uid="04AA0001").exists()
    # Her attendance from those scans is gone and the day recomputed (no day left).
    assert not DailyAttendance.objects.filter(developer=world.ada).exists()
    assert DeveloperPresence.objects.get(developer=world.ada).is_inside is False
    # Her purchases stay (money spent is spent), without the card.
    assert Purchase.objects.count() == 2
    assert not Purchase.objects.exclude(card=None).exists()
    assert DeveloperAccount.objects.get(developer=world.ada).balance == 50 - 5 - 6.5
    books_balanced()


def test_delete_a_door_reader_with_its_scans_and_log(world, admin):
    assert TCPFrameLog.objects.filter(device=world.door_in).count() == 2
    r = delete(admin, "reader", world.door_in.pk)
    assert r.status_code == 200, r.json()
    assert not RFIDDevice.objects.filter(pk=world.door_in.pk).exists()
    assert not TCPFrameLog.objects.filter(device_id=world.door_in.pk).exists()
    assert not AttendanceRecord.objects.exists() and not DailyAttendance.objects.exists()
    assert RFIDDevice.objects.filter(pk=world.door_out.pk).exists()


def test_delete_a_till_reader_keeps_its_sales(world, admin):
    Purchase.objects.update(reader=world.till)
    r = delete(admin, "reader", world.till.pk)
    assert r.status_code == 200, r.json()
    assert Purchase.objects.count() == 2 and not Purchase.objects.exclude(reader=None).exists()


def test_delete_a_seller_with_goods_stock_and_sales(world, admin):
    preview = admin.get(URL.format("seller", world.cafe.pk)).json()
    assert preview["deletes"] == {
        "service_positions": 1,
        "goods": 2,
        "stock_movements": 5,  # 2 initial + 3 sale lines
        "purchases": 2,
        "bookings": 0,
    }
    assert preview["keeps"] == {"money_transactions": 2, "readers": 1}
    assert delete(admin, "seller", world.cafe.pk).status_code == 200
    assert not Seller.objects.filter(pk=world.cafe.pk).exists()
    assert not Good.all_objects.exists() and not InventoryMovement.objects.exists()
    assert not Purchase.objects.exists()
    till = RFIDDevice.objects.get(pk=world.till.pk)
    assert till.seller is None  # the reader stays, unassigned
    # Ada's money stays as it was: she did spend it.
    assert AccountTransaction.objects.filter(account__developer=world.ada).count() == 3
    assert User.objects.filter(username="cafe").exists()
    books_balanced()


def test_delete_a_good_takes_the_purchases_that_include_it(world, admin):
    assert delete(admin, "good", world.cake.pk).status_code == 200
    assert not Good.all_objects.filter(pk=world.cake.pk).exists()
    assert Purchase.objects.count() == 1  # the tea-only purchase stays
    world.tea.refresh_from_db()
    assert world.tea.quantity == 17
    books_balanced()


def test_delete_a_service_position(world, admin):
    assert delete(admin, "position", world.counter.pk).status_code == 200
    assert not ServicePosition.all_objects.filter(pk=world.counter.pk).exists()
    assert Seller.objects.filter(pk=world.cafe.pk).exists() and not Purchase.objects.exists()
    books_balanced()


def test_delete_a_user_keeps_what_they_did(world, admin, make_user):
    finance_user = make_user(Roles.FINANCE_MANAGER, username="money")
    finance.deposit(actor=finance_user, developer=world.bob, amount="5.00", idempotency_key="x-9")
    preview = admin.get(URL.format("user", world.seller_user.pk)).json()
    assert preview["keeps"]["seller_profile"] == 1
    assert delete(admin, "user", world.seller_user.pk).status_code == 200
    assert delete(admin, "user", finance_user.pk).status_code == 200
    assert not User.objects.filter(username__in=["cafe", "money"]).exists()
    assert Seller.objects.get(pk=world.cafe.pk).user is None and Purchase.objects.count() == 2
    deposit = AccountTransaction.objects.get(idempotency_key="x-9")
    assert deposit.actor is None and deposit.amount == 5
    assert AuditLog.objects.filter(actor_username="money", actor=None).exists()
    books_balanced()


def test_not_yourself_and_not_the_last_admin(world, admin, auth_client):
    me = User.objects.get(username="root")
    r = delete(admin, "user", me.pk)
    assert r.status_code == 409 and r.json()["error"]["code"] == "CANNOT_DELETE"
    other = User.objects.create_user(username="helper", password="x")
    other_client = auth_client(other)
    from apps.accounts.models import Role, UserRole

    UserRole.objects.create(user=other, role=Role.objects.get(code=Roles.ADMIN))
    me.is_active = False
    me.save()
    r = delete(other_client, "user", other.pk)
    assert r.status_code == 409


def test_needs_the_phrase(world, admin, no_real_backup):
    r = delete(admin, "developer", world.ada.pk, confirm="delete")
    assert r.status_code == 400 and r.json()["error"]["code"] == "DELETE_NOT_CONFIRMED"
    assert Developer.objects.filter(pk=world.ada.pk).exists() and not no_real_backup


def test_no_backup_no_delete(world, admin, monkeypatch):
    def broken(prefix="x"):
        raise data_reset.BackupFailed()

    monkeypatch.setattr(data_reset, "backup_database", broken)
    assert delete(admin, "developer", world.ada.pk).status_code == 500
    assert Developer.objects.filter(pk=world.ada.pk).exists()


@pytest.mark.parametrize("role", [Roles.BOSS, Roles.MANAGER, Roles.FINANCE_MANAGER])
def test_only_admins(world, auth_client, make_user, role):
    client = auth_client(make_user(role))
    assert client.get(URL.format("developer", world.ada.pk)).status_code == 403
    assert delete(client, "developer", world.ada.pk).status_code == 403


def test_unknown(world, admin):
    assert admin.get(URL.format("planet", 1)).status_code == 404
    assert admin.get(URL.format("developer", 999999)).status_code == 404


def _till_tap(world, uid):
    event, _ = rfid.record_scan(device=world.till, uid=uid, event_time=timezone.now())
    return event if isinstance(event, RFIDEvent) else RFIDEvent.objects.latest("id")


def test_card_taps_on_purchases_dont_block_deletes(world, admin):
    """Purchases point at the card tap that paid for them (presented_event)."""
    ada_tap = _till_tap(world, "04AA0001")
    first, second = Purchase.objects.order_by("pk")
    Purchase.objects.filter(pk=first.pk).update(presented_event=ada_tap, card=world.ada_card)
    # An odd case: a sale to someone else that points at Ada's tap.
    Purchase.objects.filter(pk=second.pk).update(presented_event=ada_tap)
    assert delete(admin, "card", world.ada_card.pk).status_code == 200
    assert Purchase.objects.count() == 2 and not Purchase.objects.exclude(presented_event=None)

    bob_tap = _till_tap(world, "04BB0002")
    Purchase.objects.filter(pk=first.pk).update(presented_event=bob_tap)
    assert delete(admin, "developer", world.bob.pk).status_code == 200  # Ada's sale stays
    assert Purchase.objects.count() == 2
    books_balanced()
