import threading
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection, transaction
from django.utils import timezone

from apps.accounts.rbac import Roles
from apps.audit.models import AuditLog
from apps.developers.models import Developer, DeveloperStatus
from apps.finance import services as finance
from apps.finance.models import AccountTransaction, DeveloperAccount
from apps.goods import services as goods
from apps.goods.models import Good, InventoryMovement
from apps.purchases import services
from apps.purchases.models import Purchase
from apps.rfid.models import CardStatus, RFIDCard, RFIDCardAssignment
from apps.sellers.models import Seller, ServicePosition

PURCHASES = "/api/v1/purchases/"
PIN = "4826"


def key():
    return str(uuid.uuid4())


@pytest.fixture(autouse=True)
def manual_card_entry(settings):
    """These tests drive checkout with a typed card UID; the till-reader flow (the default
    in production) is covered in test_till_devices.py."""
    settings.PURCHASE_ALLOW_MANUAL_CARD_UID = True


@pytest.fixture
def world(db, make_user):
    """A seller with a till, two goods, and a developer with card, PIN and 50.00."""
    seller_user = make_user(Roles.SELLER, email="cafe@x.com")
    seller = Seller.objects.create(name="Cafe", user=seller_user)
    position = ServicePosition.objects.create(seller=seller, name="Counter")
    tea = Good.objects.create(service_position=position, name="Tea", price="2.50")
    cake = Good.objects.create(service_position=position, name="Cake", price="4.00")
    coffee = Good.objects.create(
        service_position=position, name="Coffee", price="3.00", track_stock=False
    )
    with transaction.atomic():
        goods.move_stock(good=tea, kind="INITIAL_STOCK", delta=10)
        goods.move_stock(good=cake, kind="INITIAL_STOCK", delta=2)

    dev_user = make_user(Roles.DEVELOPER, email="ada@x.com")
    developer = Developer.objects.create(employee_number="E1", full_name="Ada", user=dev_user)
    card = RFIDCard.objects.create(uid="04AABBCCDD")
    RFIDCardAssignment.objects.create(card=card, developer=developer)
    account = finance.open_account(developer)
    finance.deposit(actor=None, developer=developer, amount="50.00", idempotency_key="seed-0001")
    finance.set_pin(actor=None, account=account, pin=PIN, current_pin=None)

    class W:
        pass

    w = W()
    w.__dict__.update(locals())
    return w


@pytest.fixture
def till(auth_client, world):
    return auth_client(world.seller_user)


def new_purchase(client, world, *items):
    purchase = client.post(PURCHASES, {"service_position": world.position.pk}).json()
    for good, qty in items:
        client.post(f"{PURCHASES}{purchase['id']}/items/", {"good": good.pk, "quantity": qty})
    return purchase["id"]


def confirm(client, purchase_id, pin=PIN, uid="04AABBCCDD", idem=None):
    return client.post(
        f"{PURCHASES}{purchase_id}/confirm/",
        {"card_uid": uid, "pin": pin},
        HTTP_IDEMPOTENCY_KEY=idem or key(),
    )


def balance(world):
    return DeveloperAccount.objects.get(pk=world.account.pk).balance


def stock(good):
    return Good.all_objects.get(pk=good.pk).quantity


def assert_books_balanced():
    from apps.seller_finance.services import seller_ledger_mismatches

    assert finance.ledger_mismatches() == []
    assert goods.stock_mismatches() == []
    assert seller_ledger_mismatches() == []


# --- Happy path -------------------------------------------------------------------


@pytest.mark.django_db
def test_full_till_flow(till, world):
    response = till.post(PURCHASES, {"service_position": world.position.pk})
    assert response.status_code == 201, response.json()
    pid = response.json()["id"]
    assert response.json()["status"] == "DRAFT"

    till.post(f"{PURCHASES}{pid}/items/", {"good": world.tea.pk, "quantity": 2})
    till.post(f"{PURCHASES}{pid}/items/", {"good": world.tea.pk, "quantity": 1})  # merges
    body = till.post(f"{PURCHASES}{pid}/items/", {"good": world.coffee.pk}).json()
    assert [(i["good_name"], i["quantity"]) for i in body["items"]] == [("Tea", 3), ("Coffee", 1)]
    assert body["total"] == "10.50"

    response = confirm(till, pid)
    assert response.status_code == 201, response.json()
    body = response.json()
    assert body["status"] == "CONFIRMED"
    assert (body["total"], body["balance_after"]) == ("10.50", "39.50")
    assert body["developer"]["full_name"] == "Ada"

    assert balance(world) == Decimal("39.50")
    assert stock(world.tea) == 7
    txn = AccountTransaction.objects.get(kind="PURCHASE")
    assert (txn.amount, txn.reference) == (Decimal("-10.50"), f"purchase:{pid}")
    sale = InventoryMovement.objects.get(kind="SALE")
    assert (sale.good_id, sale.quantity_delta, sale.reference) == (
        world.tea.pk,
        -3,
        f"purchase:{pid}",
    )
    assert not InventoryMovement.objects.filter(good=world.coffee).exists()
    assert AuditLog.objects.filter(action="purchase.confirmed", entity_id=str(pid)).exists()
    seller_account = world.seller.account
    assert seller_account.balance == Decimal("10.50")
    assert seller_account.transactions.get().reference == f"purchase:{pid}"
    assert_books_balanced()


@pytest.mark.django_db
def test_update_and_remove_items(till, world):
    pid = new_purchase(till, world, (world.tea, 1), (world.cake, 1))
    item_id = Purchase.objects.get(pk=pid).items.get(good=world.tea).pk

    body = till.patch(f"{PURCHASES}{pid}/items/{item_id}/", {"quantity": 4}).json()
    assert body["total"] == "14.00"
    body = till.delete(f"{PURCHASES}{pid}/items/{item_id}/").json()
    assert [i["good_name"] for i in body["items"]] == ["Cake"]


@pytest.mark.django_db
def test_price_is_fixed_at_confirmation(till, world):
    pid = new_purchase(till, world, (world.tea, 2))
    Good.objects.filter(pk=world.tea.pk).update(price="3.00")
    body = confirm(till, pid).json()
    assert body["total"] == "6.00"
    assert body["items"][0]["unit_price"] == "3.00"
    Good.objects.filter(pk=world.tea.pk).update(price="9.99")
    assert till.get(f"{PURCHASES}{pid}/").json()["items"][0]["unit_price"] == "3.00"


# --- Rejections roll back everything ---------------------------------------------


@pytest.mark.django_db
def test_insufficient_balance_rolls_back(till, world):
    pid = new_purchase(till, world, (world.tea, 10), (world.cake, 2))  # 25 + 8 = 33
    Good.objects.filter(pk=world.cake.pk).update(price="20.00")  # now 25 + 40 = 65

    response = confirm(till, pid)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INSUFFICIENT_BALANCE"
    assert (balance(world), stock(world.tea), stock(world.cake)) == (Decimal("50.00"), 10, 2)
    assert Purchase.objects.get(pk=pid).status == "DRAFT"
    assert not InventoryMovement.objects.filter(kind="SALE").exists()
    assert_books_balanced()


@pytest.mark.django_db
def test_insufficient_stock_at_confirm_rolls_back(till, world):
    pid = new_purchase(till, world, (world.tea, 1), (world.cake, 2))
    with transaction.atomic():
        goods.move_stock(good=world.cake, kind="DAMAGE", delta=-1, reason="Dropped")

    response = confirm(till, pid)
    assert response.json()["error"]["code"] == "INSUFFICIENT_STOCK"
    assert (balance(world), stock(world.tea)) == (Decimal("50.00"), 10)
    assert_books_balanced()


@pytest.mark.django_db
def test_stock_hint_when_adding_items(till, world):
    pid = new_purchase(till, world)
    response = till.post(f"{PURCHASES}{pid}/items/", {"good": world.cake.pk, "quantity": 3})
    assert response.json()["error"]["details"] == {
        "good": world.cake.pk,
        "available": 2,
        "requested": 3,
    }


@pytest.mark.django_db
def test_deactivated_good_blocks_confirmation(till, world):
    pid = new_purchase(till, world, (world.tea, 1))
    Good.objects.filter(pk=world.tea.pk).update(is_active=False)
    assert confirm(till, pid).json()["error"]["code"] == "GOOD_NOT_AVAILABLE"


@pytest.mark.django_db
def test_empty_purchase_cannot_be_confirmed(till, world):
    assert confirm(till, new_purchase(till, world)).json()["error"]["code"] == "PURCHASE_EMPTY"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("setup", "code"),
    [
        ("unknown", "CARD_NOT_USABLE"),
        ("blocked", "CARD_NOT_USABLE"),
        ("unassigned", "CARD_NOT_USABLE"),
        ("suspended", "DEVELOPER_NOT_ACTIVE"),
        ("frozen", "ACCOUNT_NOT_ACTIVE"),
        ("no_pin", "PIN_NOT_SET"),
    ],
)
def test_card_and_account_checks(till, world, setup, code):
    pid = new_purchase(till, world, (world.tea, 1))
    uid = world.card.uid
    if setup == "unknown":
        uid = "0400000000"
    elif setup == "blocked":
        RFIDCard.objects.filter(pk=world.card.pk).update(status=CardStatus.BLOCKED)
    elif setup == "unassigned":
        RFIDCardAssignment.objects.update(unassigned_at=timezone.now())
    elif setup == "suspended":
        Developer.objects.filter(pk=world.developer.pk).update(status=DeveloperStatus.SUSPENDED)
    elif setup == "frozen":
        DeveloperAccount.objects.filter(pk=world.account.pk).update(status="FROZEN")
    elif setup == "no_pin":
        finance.reset_pin(actor=None, account=world.account)

    response = confirm(till, pid, uid=uid)
    assert response.json()["error"]["code"] == code
    assert (balance(world), stock(world.tea)) == (Decimal("50.00"), 10)


@pytest.mark.django_db
def test_seller_cannot_charge_own_card(auth_client, world):
    Developer.objects.filter(pk=world.developer.pk).update(user=world.seller_user)
    client = auth_client(world.seller_user)
    pid = new_purchase(client, world, (world.tea, 1))
    assert confirm(client, pid).json()["error"]["code"] == "SELF_PURCHASE_FORBIDDEN"


# --- PIN ----------------------------------------------------------------------------


@pytest.mark.django_db
def test_wrong_pin_counts_down_then_locks(till, world, settings):
    settings.PURCHASE_PIN_MAX_ATTEMPTS = 3
    pid = new_purchase(till, world, (world.tea, 1))

    r = confirm(till, pid, pin="0001")
    assert r.status_code == 400
    assert r.json()["error"]["details"] == {"attempts_remaining": 2}
    assert confirm(till, pid, pin="0002").json()["error"]["details"] == {"attempts_remaining": 1}
    r = confirm(till, pid, pin="0003")
    assert (r.status_code, r.json()["error"]["code"]) == (423, "PIN_LOCKED")
    # Locked: even the right PIN is refused.
    assert confirm(till, pid).json()["error"]["code"] == "PIN_LOCKED"
    assert AuditLog.objects.filter(action="finance.pin_locked").exists()

    DeveloperAccount.objects.filter(pk=world.account.pk).update(
        pin_locked_until=timezone.now() - timedelta(seconds=1)
    )
    assert confirm(till, pid).status_code == 201


@pytest.mark.django_db
def test_correct_pin_resets_failed_attempts(till, world):
    pid = new_purchase(till, world, (world.tea, 1))
    confirm(till, pid, pin="0001")
    confirm(till, pid)
    assert DeveloperAccount.objects.get(pk=world.account.pk).pin_failed_attempts == 0


@pytest.mark.django_db
def test_developer_sets_and_changes_pin(auth_client, world):
    client = auth_client(world.dev_user)
    url = "/api/v1/finance/accounts/me/pin/"
    assert client.post(url, {"pin": "9173"}).json()["error"]["details"] == {
        "current_pin": ["The current PIN is incorrect."]
    }
    assert client.post(url, {"pin": "9173", "current_pin": PIN}).status_code == 204
    for bad in ("1111", "1234", "9876", "12a4", "123", "1234567"):
        r = client.post(url, {"pin": bad, "current_pin": "9173"})
        assert r.status_code == 400, bad
    me = client.get("/api/v1/finance/accounts/me/").json()
    assert me["has_pin"] is True and "pin_hash" not in me
    log = AuditLog.objects.get(action="finance.pin_changed")
    assert PIN not in str(log.new_values) and "9173" not in str(log.new_values)


@pytest.mark.django_db
def test_finance_resets_forgotten_pin(auth_client, make_user, world):
    client = auth_client(make_user(Roles.FINANCE_MANAGER))
    body = client.post(f"/api/v1/finance/accounts/{world.account.pk}/reset-pin/").json()
    assert body["has_pin"] is False
    assert AuditLog.objects.filter(action="finance.pin_reset").exists()
    dev = auth_client(world.dev_user)
    assert dev.post("/api/v1/finance/accounts/me/pin/", {"pin": "5082"}).status_code == 204


# --- Idempotency and state -------------------------------------------------------------


@pytest.mark.django_db
def test_confirm_retry_is_idempotent(till, world):
    pid = new_purchase(till, world, (world.tea, 2))
    idem = key()
    first, again = confirm(till, pid, idem=idem), confirm(till, pid, idem=idem)
    assert (first.status_code, again.status_code) == (201, 200)
    assert balance(world) == Decimal("45.00")
    assert confirm(till, pid).json()["error"]["code"] == "PURCHASE_NOT_DRAFT"


@pytest.mark.django_db
def test_confirmed_purchase_is_final(till, world):
    pid = new_purchase(till, world, (world.tea, 1))
    confirm(till, pid)
    item_id = Purchase.objects.get(pk=pid).items.get().pk
    assert till.post(f"{PURCHASES}{pid}/items/", {"good": world.tea.pk}).status_code == 409
    assert till.patch(f"{PURCHASES}{pid}/items/{item_id}/", {"quantity": 5}).status_code == 409
    assert till.post(f"{PURCHASES}{pid}/cancel/").json()["error"]["code"] == "PURCHASE_NOT_DRAFT"


@pytest.mark.django_db
def test_cancel_draft(till, world):
    pid = new_purchase(till, world, (world.tea, 1))
    assert till.post(f"{PURCHASES}{pid}/cancel/").json()["status"] == "CANCELLED"
    assert confirm(till, pid).json()["error"]["code"] == "PURCHASE_NOT_DRAFT"
    assert (balance(world), stock(world.tea)) == (Decimal("50.00"), 10)


# --- Scoping ------------------------------------------------------------------------


@pytest.mark.django_db
def test_seller_isolation(auth_client, make_user, till, world):
    other_user = make_user(Roles.SELLER)
    other = Seller.objects.create(name="Other", user=other_user)
    other_pos = ServicePosition.objects.create(seller=other, name="P")
    other_good = Good.objects.create(
        service_position=other_pos, name="X", price="1.00", track_stock=False
    )

    pid = new_purchase(till, world)
    r = till.post(f"{PURCHASES}{pid}/items/", {"good": other_good.pk})
    assert r.json()["error"]["code"] == "GOOD_NOT_AVAILABLE"
    assert till.post(PURCHASES, {"service_position": other_pos.pk}).status_code == 400

    other_client = auth_client(other_user)
    assert other_client.get(f"{PURCHASES}{pid}/").status_code == 404
    assert other_client.get(PURCHASES).json()["count"] == 0


@pytest.mark.django_db
def test_developer_sees_own_purchases_only(auth_client, till, world):
    pid = new_purchase(till, world, (world.tea, 1))
    confirm(till, pid)
    client = auth_client(world.dev_user)
    assert [p["id"] for p in client.get(f"{PURCHASES}me/").json()["results"]] == [pid]
    assert client.get(PURCHASES).status_code == 403


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (Roles.ADMIN, 200),
        (Roles.FINANCE_MANAGER, 200),
        (Roles.MANAGER, 403),
        (Roles.DEVELOPER, 403),
    ],
)
def test_purchase_list_access(auth_client, make_user, world, role, expected):
    assert auth_client(make_user(role)).get(PURCHASES).status_code == expected


# --- Concurrency --------------------------------------------------------------------


def run_concurrently(fns):
    barrier = threading.Barrier(len(fns))
    outcomes = [None] * len(fns)

    def worker(i, fn):
        try:
            barrier.wait()
            outcomes[i] = fn()
        except Exception as exc:  # noqa: BLE001
            outcomes[i] = exc
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(i, f)) for i, f in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return outcomes


@pytest.mark.django_db(transaction=True)
def test_two_tills_cannot_overspend_one_balance(world):
    # Two tills (same seller) charge the same card at once; 50.00 covers only one.
    purchases = []
    for _ in range(2):
        p = services.create_purchase(actor=world.seller_user, service_position=world.position)
        services.add_item(purchase=p, good=world.tea, quantity=4)  # 10.00
        purchases.append(p)
    Good.objects.filter(pk=world.tea.pk).update(price="10.00")  # each now 40.00

    outcomes = run_concurrently(
        [
            lambda p=p: services.confirm_purchase(
                actor=world.seller_user,
                purchase=p,
                card_uid=world.card.uid,
                pin=PIN,
                idempotency_key=key(),
            )
            for p in purchases
        ]
    )

    assert sum(isinstance(o, tuple) for o in outcomes) == 1, outcomes
    assert [getattr(o, "code", None) for o in outcomes if not isinstance(o, tuple)] == [
        "INSUFFICIENT_BALANCE"
    ]
    assert balance(world) == Decimal("10.00")
    assert stock(world.tea) == 6
    assert_books_balanced()


@pytest.mark.django_db(transaction=True)
def test_two_buyers_race_for_last_items(world, make_user):
    # Two developers buy the last 2 cakes at the same moment; only one gets them.
    dev2 = Developer.objects.create(employee_number="E2", full_name="Bob")
    card2 = RFIDCard.objects.create(uid="04BBBBBBBB")
    RFIDCardAssignment.objects.create(card=card2, developer=dev2)
    acc2 = finance.open_account(dev2)
    finance.deposit(actor=None, developer=dev2, amount="50.00", idempotency_key="seed-0002")
    finance.set_pin(actor=None, account=acc2, pin="5082", current_pin=None)

    purchases = []
    for _ in range(2):
        p = services.create_purchase(actor=world.seller_user, service_position=world.position)
        services.add_item(purchase=p, good=world.cake, quantity=2)
        purchases.append(p)

    outcomes = run_concurrently(
        [
            lambda: services.confirm_purchase(
                actor=world.seller_user,
                purchase=purchases[0],
                card_uid=world.card.uid,
                pin=PIN,
                idempotency_key=key(),
            ),
            lambda: services.confirm_purchase(
                actor=world.seller_user,
                purchase=purchases[1],
                card_uid=card2.uid,
                pin="5082",
                idempotency_key=key(),
            ),
        ]
    )

    assert sum(isinstance(o, tuple) for o in outcomes) == 1, outcomes
    assert stock(world.cake) == 0
    assert DeveloperAccount.objects.get(pk=world.account.pk).balance + DeveloperAccount.objects.get(
        pk=acc2.pk
    ).balance == Decimal("92.00")
    assert_books_balanced()
