"""Cash sales at stores that accept cash, and finance's controls over them."""

import io
import threading
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from openpyxl import Workbook, load_workbook
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserRole
from apps.accounts.rbac import ROLES, Roles
from apps.audit.models import AuditLog
from apps.cash import services as cash
from apps.cash.models import CashEntry, CashRequest, StockCount
from apps.finance import services as finance
from apps.finance.models import AccountTransaction
from apps.goods import services as goods
from apps.goods.models import Good, GoodKind
from apps.purchases import services
from apps.purchases.models import Payment, Purchase, PurchaseStatus
from apps.rfid.models import Building
from apps.sellers.models import Seller, ServicePosition
from tests import test_purchases

world = test_purchases.world
till = test_purchases.till
manual_card_entry = test_purchases.manual_card_entry
new_purchase = test_purchases.new_purchase
stock = test_purchases.stock
key = test_purchases.key
PURCHASES = test_purchases.PURCHASES

GOODS = "/api/v1/goods/"
STORES = "/api/v1/cash/stores/"
ENTRIES = "/api/v1/cash/entries/"
REQUESTS = "/api/v1/cash/requests/"
IMPORT_GOODS = "/api/v1/imports/goods/"
COUNTS = "/api/v1/cash/stock-counts/"


def client_of(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def shop(world):
    """The Cafe of `world`, taking cash."""
    world.seller.accepts_cash = True
    world.seller.save()
    return world


@pytest.fixture
def finance_client(make_user):
    return client_of(make_user(Roles.FINANCE_MANAGER, username="fin"))


@pytest.fixture
def admin_client(admin):
    return client_of(admin)


def pay_cash(client, pid, buyer="", idem=None):
    return client.post(
        f"{PURCHASES}{pid}/confirm-cash/",
        {"buyer_name": buyer},
        HTTP_IDEMPOTENCY_KEY=idem or key(),
    )


def sell(client, shop, *items, buyer=""):
    pid = new_purchase(client, shop, *items)
    response = pay_cash(client, pid, buyer)
    assert response.status_code == 201, response.json()
    return pid


def handover(client, shop, amount, note="", idem=None):
    return client.post(
        f"{ENTRIES}handover/",
        {"seller": shop.seller.pk, "amount": amount, "note": note},
        HTTP_IDEMPOTENCY_KEY=idem or key(),
    )


def ask_delete(client, pid, reason="Wrong goods"):
    return client.post(REQUESTS, {"purchase": pid, "reason": reason})


def code(response):
    return response.status_code, response.json()["error"]["code"]


def assert_books():
    assert finance.ledger_mismatches() == []
    assert goods.stock_mismatches() == []
    assert cash.ledger_mismatches() == []


# --- Selling for cash ---------------------------------------------------------------------


@pytest.mark.django_db
def test_cash_sale(till, shop):
    pid = new_purchase(till, shop, (shop.tea, 2), (shop.coffee, 1))
    body = pay_cash(till, pid, " Visitor Kim ").json()
    assert (body["status"], body["payment"], body["total"]) == ("CONFIRMED", "CASH", "8.00")
    assert body["buyer_name"] == "Visitor Kim"
    assert body["developer"] is None and body["card_uid"] is None and body["balance_after"] is None
    assert stock(shop.tea) == 8
    assert test_purchases.balance(shop) == Decimal("50.00")  # no wallet is touched
    assert not AccountTransaction.objects.filter(reference=f"purchase:{pid}").exists()
    entry = CashEntry.objects.get()
    assert (entry.kind, entry.amount, entry.owed_after) == ("SALE", Decimal("8.00"), Decimal("8"))
    assert entry.actor == shop.seller_user and cash.owed(shop.seller.pk) == Decimal("8.00")
    assert AuditLog.objects.filter(action="purchase.paid_cash").count() == 1
    assert_books()


@pytest.mark.django_db
def test_a_store_without_cash_sells_by_card_only(till, world):
    pid = new_purchase(till, world, (world.tea, 1))
    assert code(pay_cash(till, pid)) == (409, "CASH_NOT_ACCEPTED")
    assert stock(world.tea) == 10 and not CashEntry.objects.exists()
    assert test_purchases.confirm(till, pid).status_code == 201


@pytest.mark.django_db
def test_a_retry_or_second_click_sells_once(till, shop):
    pid = new_purchase(till, shop, (shop.tea, 1))
    idem = key()
    assert pay_cash(till, pid, idem=idem).status_code == 201
    assert pay_cash(till, pid, idem=idem).status_code == 200
    assert code(pay_cash(till, pid)) == (409, "PURCHASE_NOT_DRAFT")
    assert CashEntry.objects.count() == 1 and stock(shop.tea) == 9
    other = new_purchase(till, shop, (shop.tea, 1))
    assert code(pay_cash(till, other, idem=idem))[1] == "IDEMPOTENCY_KEY_REUSED"
    assert till.post(f"{PURCHASES}{other}/confirm-cash/").status_code == 400  # no key


@pytest.mark.django_db
def test_the_key_of_a_wallet_payment_is_not_a_cash_payment(till, shop):
    pid = new_purchase(till, shop, (shop.tea, 1))
    idem = key()
    assert test_purchases.confirm(till, pid, idem=idem).status_code == 201
    assert code(pay_cash(till, pid, idem=idem))[1] == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.django_db
def test_nothing_is_sold_without_stock_or_goods(till, shop):
    pid = new_purchase(till, shop, (shop.cake, 2))
    with transaction.atomic():
        goods.move_stock(good=shop.cake, kind="DAMAGE", delta=-1)
    assert code(pay_cash(till, pid)) == (409, "INSUFFICIENT_STOCK")
    assert Purchase.objects.get(pk=pid).status == PurchaseStatus.DRAFT
    empty = new_purchase(till, shop)
    assert code(pay_cash(till, empty))[1] == "PURCHASE_EMPTY"
    free = Good.objects.create(
        service_position=shop.position, name="Free", price="0", track_stock=False
    )
    pid = new_purchase(till, shop, (free, 1))
    assert code(pay_cash(till, pid))[1] == "PURCHASE_EMPTY"
    assert not CashEntry.objects.exists()


@pytest.mark.django_db
def test_who_may_take_cash(shop, make_user, admin_client):
    other = make_user(Roles.GOODS_SELLER, username="other")
    Seller.objects.create(name="Other", user=other)
    pid = new_purchase(client_of(shop.seller_user), shop, (shop.tea, 1))
    assert pay_cash(client_of(other), pid).status_code in (403, 404)
    # Staff need purchase.cash; purchase.confirm is not enough.
    role = Role.objects.create(code="CLERK", name="Clerk")
    from apps.accounts.models import Permission

    role.permissions.add(
        *Permission.objects.filter(codename__in=["purchase.view", "purchase.confirm"])
    )
    clerk = make_user(username="clerk")
    UserRole.objects.create(user=clerk, role=role)
    assert pay_cash(client_of(clerk), pid).status_code == 403
    assert pay_cash(admin_client, pid).status_code == 201


@pytest.mark.django_db
def test_counter_managers_take_cash_at_their_counter(shop, make_user):
    manager = make_user(Roles.GOODS_SELLER, username="mgr")
    counter = ServicePosition.objects.create(seller=shop.seller, name="Two", manager=manager)
    juice = Good.objects.create(
        service_position=counter, name="Juice", price="2", track_stock=False
    )
    client = client_of(manager)
    pid = client.post(PURCHASES, {"service_position": counter.pk}).json()["id"]
    client.post(f"{PURCHASES}{pid}/items/", {"good": juice.pk, "quantity": 1})
    assert pay_cash(client, pid).status_code == 201
    assert ask_delete(client, pid).status_code == 201
    mine = client.get(REQUESTS).json()["results"]
    assert [r["purchase"] for r in mine] == [pid]
    # The store's whole cash is not theirs to see.
    assert client.get(STORES).status_code == 403
    theirs = sell(client_of(shop.seller_user), shop, (shop.tea, 1))
    assert ask_delete(client, theirs).status_code == 400


@pytest.mark.django_db
def test_a_court_booking_is_never_paid_in_cash(shop, till):
    draft = Purchase.objects.create(
        seller=shop.seller, service_position=shop.position, kind="BOOKING"
    )
    with pytest.raises(Exception) as caught:
        services.confirm_cash_sale(actor=shop.seller_user, purchase=draft, idempotency_key=key())
    assert caught.value.code == "GOOD_NOT_AVAILABLE"


@pytest.mark.django_db
def test_the_database_refuses_half_cash_purchases(shop):
    draft = Purchase.objects.create(seller=shop.seller, service_position=shop.position)
    for values in (
        {"payment": "CASH", "developer": shop.developer},
        {"payment": "CASH", "kind": "BOOKING"},
        {"payment": "CASH", "status": "CONFIRMED", "confirmed_at": timezone.now()},
        {"payment": "WALLET", "status": "CONFIRMED", "total": 1, "confirmed_at": timezone.now()},
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            Purchase.objects.filter(pk=draft.pk).update(**values)


@pytest.mark.django_db
def test_a_cash_sale_takes_no_card_afterwards(till, shop):
    pid = sell(till, shop, (shop.tea, 1))
    for path in ("wait", "renew-wait", "stop-waiting"):
        assert code(till.post(f"{PURCHASES}{pid}/{path}/")) == (409, "PURCHASE_NOT_DRAFT")


# --- The ledger and handovers -------------------------------------------------------------


@pytest.mark.django_db
def test_the_ledger_is_append_only(till, shop):
    sell(till, shop, (shop.tea, 1))
    entry = CashEntry.objects.get()
    with pytest.raises(TypeError):
        entry.save()
    for sql in ("UPDATE cash_cashentry SET amount = 1", "DELETE FROM cash_cashentry"):
        with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql)


@pytest.mark.django_db
def test_finance_records_a_handover(till, shop, finance_client):
    sell(till, shop, (shop.tea, 4))
    idem = key()
    response = handover(finance_client, shop, "6.00", idem=idem)
    assert response.status_code == 201, response.json()
    body = response.json()
    assert (body["kind"], body["amount"], body["owed_after"]) == ("HANDOVER", "-6.00", "4.00")
    assert body["actor_username"] == "fin"
    assert handover(finance_client, shop, "6.00", idem=idem).status_code == 200
    assert code(handover(finance_client, shop, "5.00", idem=idem))[1] == "IDEMPOTENCY_KEY_REUSED"
    assert cash.owed(shop.seller.pk) == Decimal("4.00")
    assert AuditLog.objects.filter(action="cash.handed_over").count() == 1
    assert_books()


@pytest.mark.django_db
def test_more_than_owed_needs_a_note(till, shop, finance_client):
    sell(till, shop, (shop.tea, 1))
    assert handover(finance_client, shop, "9.00").status_code == 400
    assert handover(finance_client, shop, "0").status_code == 400
    assert handover(finance_client, shop, "-1").status_code == 400
    assert handover(finance_client, shop, "9.00", note="Found in drawer").status_code == 201
    assert cash.owed(shop.seller.pk) == Decimal("-6.50")


@pytest.mark.django_db
def test_only_finance_records_handovers(till, shop, make_user):
    sell(till, shop, (shop.tea, 1))
    for role in (Roles.BOSS, Roles.MANAGER, Roles.BUILDING_OWNER, Roles.BUILDING_MANAGER):
        assert handover(client_of(make_user(role)), shop, "1").status_code == 403
    assert handover(till, shop, "1").status_code == 403
    # Not even finance, for a store they work for.
    UserRole.objects.create(
        user=shop.seller_user, role=Role.objects.get(code=Roles.FINANCE_MANAGER)
    )
    fresh = type(shop.seller_user).objects.get(pk=shop.seller_user.pk)
    assert code(handover(client_of(fresh), shop, "1")) == (403, "OWN_STORE")
    assert cash.owed(shop.seller.pk) == Decimal("2.50")


@pytest.mark.django_db
def test_a_wrong_handover_is_cancelled_once(till, shop, finance_client):
    sell(till, shop, (shop.tea, 4))
    wrong = handover(finance_client, shop, "8.00").json()["id"]
    url = f"{ENTRIES}{wrong}/reverse/"

    def reverse(client, pk=wrong, idem=None):
        return client.post(
            f"{ENTRIES}{pk}/reverse/",
            {"reason": "Typed wrong"},
            HTTP_IDEMPOTENCY_KEY=idem or key(),
        )

    assert finance_client.post(url, {}, HTTP_IDEMPOTENCY_KEY=key()).status_code == 400
    assert reverse(till).status_code == 403
    idem = key()
    back = reverse(finance_client, idem=idem)
    assert back.status_code == 201 and back.json()["owed_after"] == "10.00"
    assert reverse(finance_client, idem=idem).status_code == 200
    assert code(reverse(finance_client)) == (409, "NOT_REVERSIBLE")
    assert code(reverse(finance_client, back.json()["id"])) == (409, "NOT_REVERSIBLE")
    sale = CashEntry.objects.get(kind="SALE")
    assert code(reverse(finance_client, sale.pk)) == (409, "NOT_REVERSIBLE")
    row = next(r for r in finance_client.get(STORES).json()["stores"])
    assert (row["handed_over"], row["owed"], row["last_handover_at"]) == ("0.00", "10.00", None)
    assert_books()


# --- Deleting a cash sale -----------------------------------------------------------------


@pytest.mark.django_db
def test_a_cash_sale_is_neither_changed_nor_deleted_alone(till, shop, admin_client):
    pid = sell(till, shop, (shop.tea, 2))
    for client in (till, admin_client):
        assert code(client.delete(f"{PURCHASES}{pid}/")) == (409, "CASH_DELETE_NEEDS_APPROVAL")
        change = {"items": [{"good": shop.tea.pk, "quantity": 1}]}
        response = client.patch(f"{PURCHASES}{pid}/", change, format="json")
        assert code(response) == (409, "CASH_SALE_FIXED")
    assert stock(shop.tea) == 8 and cash.owed(shop.seller.pk) == Decimal("5.00")


@pytest.mark.django_db
def test_deleting_waits_for_finance(till, shop, finance_client):
    pid = sell(till, shop, (shop.tea, 2))
    assert till.post(REQUESTS, {"purchase": pid, "reason": " "}).status_code == 400
    made = ask_delete(till, pid)
    assert made.status_code == 201 and made.json()["status"] == "PENDING"
    assert code(ask_delete(till, pid)) == (409, "ALREADY_REQUESTED")
    assert Purchase.objects.get(pk=pid).status == "CONFIRMED"
    shown = till.get(f"{PURCHASES}{pid}/").json()["delete_request"]
    assert (shown["status"], shown["reason"]) == ("PENDING", "Wrong goods")

    url = f"{REQUESTS}{made.json()['id']}/"
    assert till.post(f"{url}approve/").status_code == 403
    assert till.post(f"{url}reject/", {"note": "x"}).status_code == 403
    row = finance_client.get(REQUESTS, {"status": "PENDING"}).json()["results"][0]
    assert (row["total"], row["sold_by"], row["requested_by_username"]) == ("5.00", "cafe", "cafe")
    assert finance_client.post(f"{url}approve/", {"note": "ok"}).json()["status"] == "APPROVED"

    purchase = Purchase.objects.get(pk=pid)
    assert purchase.status == "REFUNDED" and purchase.refund_reason == "Wrong goods"
    assert purchase.refunded_by.username == "fin"
    assert stock(shop.tea) == 10 and cash.owed(shop.seller.pk) == 0
    assert code(finance_client.post(f"{url}approve/")) == (409, "REQUEST_CLOSED")
    assert code(ask_delete(till, pid))[1] == "REQUEST_CLOSED"
    assert_books()


@pytest.mark.django_db
def test_a_rejected_request_leaves_the_sale(till, shop, finance_client):
    pid = sell(till, shop, (shop.tea, 2))
    url = f"{REQUESTS}{ask_delete(till, pid).json()['id']}/reject/"
    assert finance_client.post(url, {}).status_code == 400  # why not?
    assert finance_client.post(url, {"note": "Show the goods"}).json()["status"] == "REJECTED"
    shown = till.get(f"{PURCHASES}{pid}/").json()["delete_request"]
    assert (shown["status"], shown["decision_note"]) == ("REJECTED", "Show the goods")
    assert cash.owed(shop.seller.pk) == Decimal("5.00")
    assert ask_delete(till, pid).status_code == 201  # may ask again


@pytest.mark.django_db
def test_finance_deletes_at_once_but_not_its_own_sale(till, shop, finance_client, admin_client):
    pid = sell(till, shop, (shop.tea, 1))
    made = ask_delete(finance_client, pid).json()
    assert (made["status"], made["decided_by_username"]) == ("APPROVED", "fin")
    assert Purchase.objects.get(pk=pid).status == "REFUNDED"
    # The admin took the cash: someone else approves.
    own = sell(admin_client, shop, (shop.tea, 1))
    made = ask_delete(admin_client, own).json()
    assert made["status"] == "PENDING"
    url = f"{REQUESTS}{made['id']}/approve/"
    assert code(admin_client.post(url)) == (403, "OWN_REQUEST")
    assert finance_client.post(url).status_code == 200
    assert_books()


@pytest.mark.django_db
def test_who_took_the_cash_cannot_approve_anothers_request(till, shop, admin_client):
    own = sell(admin_client, shop, (shop.tea, 1))
    made = ask_delete(till, own).json()
    assert code(admin_client.post(f"{REQUESTS}{made['id']}/approve/")) == (403, "OWN_REQUEST")


@pytest.mark.django_db
def test_a_store_login_with_finance_rights_cannot_approve(till, shop):
    pid = sell(till, shop, (shop.tea, 1))
    UserRole.objects.create(
        user=shop.seller_user, role=Role.objects.get(code=Roles.FINANCE_MANAGER)
    )
    fresh = client_of(type(shop.seller_user).objects.get(pk=shop.seller_user.pk))
    made = ask_delete(fresh, pid).json()
    assert made["status"] == "PENDING"
    assert code(fresh.post(f"{REQUESTS}{made['id']}/approve/")) == (403, "OWN_STORE")


@pytest.mark.django_db
def test_wallet_sales_are_not_deleted_through_requests(till, shop):
    pid = new_purchase(till, shop, (shop.tea, 1))
    test_purchases.confirm(till, pid)
    assert code(ask_delete(till, pid)) == (409, "CASH_SALE_FIXED")
    assert till.delete(f"{PURCHASES}{pid}/").status_code == 200  # as before


@pytest.mark.django_db
def test_withdrawing(till, shop, finance_client):
    pid = sell(till, shop, (shop.tea, 1))
    url = f"{REQUESTS}{ask_delete(till, pid).json()['id']}/withdraw/"
    assert code(finance_client.post(url)) == (403, "OWN_REQUEST")
    assert till.post(url).json()["status"] == "WITHDRAWN"


# --- Prices and goods ---------------------------------------------------------------------


def price(good):
    return Good.all_objects.get(pk=good.pk).price


@pytest.mark.django_db
def test_a_price_waits_for_approval(till, shop, finance_client):
    response = till.patch(f"{GOODS}{shop.tea.pk}/", {"price": "0.50", "name": "Green tea"})
    assert response.status_code == 200, response.json()
    body = response.json()
    assert (body["price"], body["name"]) == ("2.50", "Green tea")
    assert body["pending_request"]["new_price"] == "0.50"
    # A newer wish replaces it; the price as it is, is none.
    till.patch(f"{GOODS}{shop.tea.pk}/", {"price": "3.00"})
    till.patch(f"{GOODS}{shop.tea.pk}/", {"price": "2.50", "description": "x"})
    waiting = finance_client.get(REQUESTS, {"status": "PENDING"}).json()["results"]
    assert [(r["old_price"], r["new_price"]) for r in waiting] == [("2.50", "3.00")]
    pid = sell(till, shop, (shop.tea, 1))
    assert Purchase.objects.get(pk=pid).total == Decimal("2.50")
    assert till.post(f"{REQUESTS}{waiting[0]['id']}/approve/").status_code == 403
    assert finance_client.post(f"{REQUESTS}{waiting[0]['id']}/approve/").status_code == 200
    assert price(shop.tea) == Decimal("3.00")
    assert till.get(f"{GOODS}{shop.tea.pk}/").json()["pending_request"] is None


@pytest.mark.django_db
def test_staff_and_stores_without_cash_set_prices_directly(till, world, admin_client):
    assert till.patch(f"{GOODS}{world.tea.pk}/", {"price": "3"}).json()["price"] == "3.00"
    world.seller.accepts_cash = True
    world.seller.save()
    assert admin_client.patch(f"{GOODS}{world.tea.pk}/", {"price": "4"}).json()["price"] == "4.00"
    assert not CashRequest.objects.exists()


@pytest.mark.django_db
def test_a_new_good_waits_off_sale(till, shop, finance_client):
    data = {"service_position": shop.position.pk, "name": "Cheap", "price": "0.10"}
    made = till.post(GOODS, data | {"initial_quantity": 5, "is_active": True}).json()
    assert made["is_active"] is False and made["pending_request"]["kind"] == "NEW_GOOD"
    url = f"{GOODS}{made['id']}/"
    assert code(till.patch(url, {"is_active": True})) == (409, "AWAITING_APPROVAL")
    pid = new_purchase(till, shop)
    add = till.post(f"{PURCHASES}{pid}/items/", {"good": made["id"], "quantity": 1})
    assert code(add)[1] == "GOOD_NOT_AVAILABLE"
    request = CashRequest.objects.get()
    reject = finance_client.post(f"{REQUESTS}{request.pk}/reject/", {"note": "Too cheap"})
    assert reject.status_code == 200
    assert code(till.patch(url, {"is_active": True})) == (409, "AWAITING_APPROVAL")
    # A better price is a new request; approving puts it on sale.
    assert till.patch(url, {"price": "1.00"}).json()["price"] == "1.00"
    request = CashRequest.objects.get(status="PENDING")
    assert (request.kind, request.new_price) == ("NEW_GOOD", Decimal("1.00"))
    assert finance_client.post(f"{REQUESTS}{request.pk}/approve/").status_code == 200
    good = till.get(url).json()
    assert (good["is_active"], good["price"]) == (True, "1.00")
    # Approved once: the store switches it off and on itself, prices wait again.
    till.patch(url, {"is_active": False})
    assert till.patch(url, {"is_active": True}).status_code == 200
    assert till.patch(url, {"price": "0.10"}).json()["price"] == "1.00"


@pytest.mark.django_db
def test_stock_is_added_but_not_written_off_by_the_store(till, shop, admin_client):
    url = f"{GOODS}{shop.tea.pk}/stock/"
    damage = {"kind": "DAMAGE", "quantity": 1, "reason": "Broken"}
    count = {"kind": "ADJUSTMENT", "counted_quantity": 3, "reason": "Count"}
    assert code(till.post(url, damage)) == (403, "CASH_STORE_LOCKED")
    assert code(till.post(url, count)) == (403, "CASH_STORE_LOCKED")
    assert till.post(url, {"kind": "RESTOCK", "quantity": 5}).status_code == 201
    assert code(till.patch(f"{GOODS}{shop.tea.pk}/", {"track_stock": False})) == (
        403,
        "CASH_STORE_LOCKED",
    )
    assert code(till.delete(f"{GOODS}{shop.tea.pk}/")) == (403, "CASH_STORE_LOCKED")
    assert stock(shop.tea) == 15
    assert admin_client.post(url, damage).status_code == 201
    assert till.delete(f"{GOODS}{shop.coffee.pk}/").status_code == 204  # no stock to hide


@pytest.mark.django_db
def test_the_excel_import_is_no_way_around(shop, make_user):
    """A store's own login importing goods: the same rules as on the pages."""
    role = Role.objects.create(code="IMPORT", name="Import")
    from apps.accounts.models import Permission

    role.permissions.add(Permission.objects.get(codename="excel.import_goods"))
    UserRole.objects.create(user=shop.seller_user, role=role)
    client = client_of(type(shop.seller_user).objects.get(pk=shop.seller_user.pk))

    def load(*row):
        book = Workbook()
        sheet = book.active
        sheet.append(["good", "price", "quantity"])
        sheet.append(list(row))
        file = io.BytesIO()
        book.save(file)
        file.seek(0)
        file.name = "goods.xlsx"
        return client.post(IMPORT_GOODS, {"file": file, "dry_run": "false"}, format="multipart")

    assert load("Tea", "0.10", "").status_code == 200
    assert price(shop.tea) == Decimal("2.50")
    assert CashRequest.objects.get().new_price == Decimal("0.10")
    load("Tea", "", 1)
    assert stock(shop.tea) == 10
    load("Candy", "0.10", "")
    assert Good.objects.get(name="Candy").is_active is False


@pytest.mark.django_db
def test_courts_are_not_locked(shop):
    court = Good.objects.create(
        service_position=shop.position,
        name="Court",
        price="5",
        kind=GoodKind.RENTAL,
        track_stock=False,
    )
    changed = cash.update_store_good(actor=shop.seller_user, good=court, price=Decimal("6"))
    assert changed.price == Decimal("6.00") and not CashRequest.objects.exists()


# --- Stock counts -------------------------------------------------------------------------


def count_lines(client, shop, *lines):
    body = {
        "seller": shop.seller.pk,
        "lines": [{"good": g.pk, "expected": e, "counted": c} for g, e, c in lines],
    }
    return client.post(COUNTS, body, format="json")


@pytest.mark.django_db
def test_counting_the_stock(till, shop, finance_client):
    sheet = finance_client.get(f"{COUNTS}sheet/", {"seller": shop.seller.pk}).json()
    assert [(g["name"], g["quantity"]) for g in sheet] == [("Cake", 2), ("Tea", 10)]
    response = count_lines(finance_client, shop, (shop.tea, 10, 7), (shop.cake, 2, 3))
    assert response.status_code == 201, response.json()
    body = response.json()
    assert (body["missing_value"], body["surplus_value"], body["outcome"]) == (
        "7.50",
        "4.00",
        "OPEN",
    )
    assert stock(shop.tea) == 7 and stock(shop.cake) == 3
    row = finance_client.get(STORES).json()["stores"][0]
    assert (row["open_counts"], row["open_missing"], row["owed"]) == (1, "7.50", "0.00")
    assert_books()

    url = f"{COUNTS}{body['id']}/resolve/"
    assert finance_client.post(url, {"outcome": "CHARGED"}).status_code == 400  # say why
    too_much = {"outcome": "CHARGED", "note": "x", "amount": "8"}
    assert finance_client.post(url, too_much).status_code == 400
    charged = finance_client.post(url, {"outcome": "CHARGED", "note": "No reason", "amount": "5"})
    assert charged.json()["charged"] == "5.00" and cash.owed(shop.seller.pk) == Decimal("5.00")
    assert code(finance_client.post(url, {"outcome": "EXPLAINED", "note": "x"})) == (
        409,
        "COUNT_RESOLVED",
    )
    # Cancelling the charge opens the count again.
    back = finance_client.post(
        f"{ENTRIES}{charged.json()['charge']}/reverse/",
        {"reason": "Found them"},
        HTTP_IDEMPOTENCY_KEY=key(),
    )
    assert back.status_code == 201 and cash.owed(shop.seller.pk) == 0
    assert StockCount.objects.get().outcome == "OPEN"
    done = finance_client.post(url, {"outcome": "EXPLAINED", "note": "Found in the fridge"})
    assert done.json()["outcome"] == "EXPLAINED"
    assert_books()


@pytest.mark.django_db
def test_a_count_of_goods_sold_meanwhile_is_counted_again(till, shop, finance_client):
    sell(till, shop, (shop.tea, 1))
    response = count_lines(finance_client, shop, (shop.tea, 10, 9), (shop.cake, 2, 2))
    assert code(response) == (409, "STOCK_MOVED")
    assert response.json()["error"]["details"]["goods"] == [shop.tea.pk]
    assert not StockCount.objects.exists()
    assert count_lines(finance_client, shop, (shop.tea, 9, 9)).json()["outcome"] == "MATCHED"


@pytest.mark.django_db
def test_the_store_does_not_count_itself(till, shop, make_user, finance_client):
    assert count_lines(till, shop, (shop.tea, 10, 1)).status_code == 403
    assert till.get(f"{COUNTS}sheet/", {"seller": shop.seller.pk}).status_code == 403
    assert count_lines(client_of(make_user(Roles.BOSS)), shop, (shop.tea, 10, 1)).status_code == 403
    UserRole.objects.create(
        user=shop.seller_user, role=Role.objects.get(code=Roles.FINANCE_MANAGER)
    )
    fresh = client_of(type(shop.seller_user).objects.get(pk=shop.seller_user.pk))
    assert code(count_lines(fresh, shop, (shop.tea, 10, 1))) == (403, "OWN_STORE")
    # Other stores' goods, goods without stock, and the same good twice are refused.
    assert count_lines(finance_client, shop, (shop.coffee, 0, 0)).status_code == 400
    assert (
        count_lines(finance_client, shop, (shop.tea, 10, 9), (shop.tea, 10, 8)).status_code == 400
    )
    assert count_lines(finance_client, shop, (shop.tea, 10, "9.5")).status_code == 400
    assert stock(shop.tea) == 10


# --- What finance and the store see -------------------------------------------------------


@pytest.mark.django_db
def test_returned_cash_counts_on_the_day_it_is_returned(till, shop, finance_client):
    """Yesterday's sale deleted today: yesterday's cash stays as it was, today's drops."""
    pid = sell(till, shop, (shop.tea, 2))
    yesterday = timezone.now() - timedelta(days=1)
    with connection.cursor() as cursor:  # move the sale to yesterday
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute("ALTER TABLE cash_cashentry DISABLE TRIGGER cash_cashentry_append_only")
        cursor.execute("UPDATE cash_cashentry SET created_at = %s", [yesterday])
        cursor.execute("ALTER TABLE cash_cashentry ENABLE TRIGGER cash_cashentry_append_only")
    sell(till, shop, (shop.tea, 3))
    ask_delete(finance_client, pid)
    handover(finance_client, shop, "1.00")
    body = finance_client.get(f"{STORES}{shop.seller.pk}/").json()
    assert [
        (d["sales"], d["returned"], d["expected"], d["handed_over"], d["owed"])
        for d in body["daily"]
    ] == [
        ("7.50", "5.00", "2.50", "1.00", "6.50"),
        ("5.00", "0.00", "5.00", "0.00", "5.00"),
    ]
    today = timezone.localdate().isoformat()
    row = finance_client.get(STORES, {"date_from": today, "date_to": today}).json()["stores"][0]
    assert (row["opening"], row["expected"], row["closing"], row["owed"]) == (
        "5.00",
        "2.50",
        "6.50",
        "6.50",
    )


@pytest.mark.django_db
def test_stores_see_their_own_cash_only(till, shop, make_user, finance_client):
    sell(till, shop, (shop.tea, 1))
    other = make_user(Roles.GOODS_SELLER, username="other")
    Seller.objects.create(name="Other", user=other, accepts_cash=True)
    assert [s["name"] for s in till.get(STORES).json()["stores"]] == ["Cafe"]
    theirs = client_of(other)
    assert [s["name"] for s in theirs.get(STORES).json()["stores"]] == ["Other"]
    assert theirs.get(f"{STORES}{shop.seller.pk}/").status_code == 404
    assert theirs.get(ENTRIES).json()["count"] == 0
    assert theirs.get(REQUESTS).json()["count"] == 0
    names = {s["name"] for s in finance_client.get(STORES).json()["stores"]}
    assert names == {"Cafe", "Other"}
    boss = client_of(make_user(Roles.BOSS))
    assert boss.get(STORES).status_code == 200
    for role in (Roles.MANAGER, Roles.DEVELOPER, Roles.BUILDING_OWNER, Roles.PLAYGROUND_SELLER):
        assert client_of(make_user(role)).get(STORES).status_code == 403
    assert APIClient().get(STORES).status_code == 401


@pytest.mark.django_db
def test_a_store_that_stopped_taking_cash_still_owes(till, shop, finance_client):
    sell(till, shop, (shop.tea, 1))
    Seller.objects.filter(pk=shop.seller.pk).update(accepts_cash=False)
    row = finance_client.get(STORES).json()["stores"][0]
    assert (row["accepts_cash"], row["owed"]) == (False, "2.50")
    assert handover(finance_client, shop, "2.50").status_code == 201


@pytest.mark.django_db
def test_only_admins_switch_cash_on(shop, admin_client, make_user):
    role = Role.objects.create(code="STORES", name="Stores")
    from apps.accounts.models import Permission

    role.permissions.add(
        *Permission.objects.filter(codename__in=["seller.view", "seller.update", "seller.create"])
    )
    user = make_user(username="stores")
    UserRole.objects.create(user=user, role=role)
    client = client_of(user)
    url = f"/api/v1/sellers/{shop.seller.pk}/"
    assert client.patch(url, {"accepts_cash": False}).status_code == 403
    assert client.patch(url, {"accepts_cash": True, "notes": "x"}).status_code == 200  # no change
    made = client.post("/api/v1/sellers/", {"name": "New", "accepts_cash": True})
    assert made.status_code == 403
    assert admin_client.patch(url, {"accepts_cash": False}).json()["accepts_cash"] is False
    log = AuditLog.objects.filter(action="seller.updated").latest("id")
    assert log.new_values == {"accepts_cash": False}


def test_roles():
    fin = ROLES[Roles.FINANCE_MANAGER].permissions
    assert {"cash.view", "cash.handover", "cash.approve", "cash.count"} <= fin
    assert "cash.enable" not in fin and "purchase.cash" not in fin
    assert "cash.view" in ROLES[Roles.BOSS].permissions
    assert "cash.view" not in ROLES[Roles.BUILDING_OWNER].permissions


@pytest.mark.django_db
def test_figures_split_cash_and_wallet(till, shop, make_user, finance_client, admin_client):
    building = Building.objects.create(code="B1", name="One")
    shop.position.building = building
    shop.position.save()
    sell(till, shop, (shop.tea, 2), buyer="Kim")
    pid = new_purchase(till, shop, (shop.cake, 1))
    test_purchases.confirm(till, pid)

    summary = till.get(f"{PURCHASES}summary/").json()
    assert (summary["paid_total"], summary["cash_total"], summary["wallet_total"]) == (
        "9.00",
        "5.00",
        "4.00",
    )
    assert (summary["cash_count"], summary["wallet_count"], summary["buyers"]) == (1, 1, 1)
    assert till.get(PURCHASES, {"payment": "CASH"}).json()["count"] == 1
    assert till.get(PURCHASES, {"search": "kim"}).json()["count"] == 1
    sold = {r["name"]: (r["money"], r["cash"]) for r in till.get(f"{GOODS}statistics/").json()}
    assert sold["Tea"] == ("5.00", "5.00") and sold["Cake"] == ("4.00", "0.00")

    stats = finance_client.get("/api/v1/stats/finance/").json()
    assert stats["money"]["store_sales"]["cash_total"] == "5.00"
    assert stats["money"]["spending"]["total"] == "4.00"  # wallets only
    row = next(s for s in stats["sellers"] if s["name"] == "Cafe")
    assert (row["sales_total"], row["cash_total"]) == ("9.00", "5.00")
    assert [(p["cash"], p["total"]) for p in row["by_building"]] == [
        (False, "4.00"),
        (True, "5.00"),
    ]
    # Filtered by building: the cash taken at its counters.
    one = finance_client.get("/api/v1/stats/finance/", {"building": building.pk}).json()
    assert one["money"]["store_sales"]["cash_total"] == "5.00"
    assert one["sellers"][0]["sales_total"] == "5.00"

    book = load_workbook(io.BytesIO(finance_client.get(f"{STORES}export/").content))
    assert len(book.worksheets) == 5
    assert book.worksheets[0]["A2"].value == "Cafe"
    book = load_workbook(io.BytesIO(admin_client.get(f"{PURCHASES}export/").content))
    rows = list(book.worksheets[0].values)
    at = rows[0].index("Payment")
    assert sorted(r[at] for r in rows[1:]) == ["Cash", "Wallet"]
    assert finance_client.get("/api/v1/excel/export/finance-stats/").status_code in (200, 404)
    report = admin_client.get(f"{GOODS}statistics/export/")
    figures = {r[0]: r[3] for r in load_workbook(io.BytesIO(report.content)).worksheets[0].values}
    assert (figures["Paid in cash"], figures["Paid by wallet"]) == (5, 4)


@pytest.mark.django_db
def test_cash_does_not_earn_or_use_preferential_service(till, shop):
    from apps.purchases import preferential

    sell(till, shop, (shop.tea, 2))
    item = Purchase.objects.get(payment=Payment.CASH).items.get()
    assert item.free_quantity == 0
    assert preferential.usage_last_month(shop.developer.pk) == 0


# --- Deleting for good --------------------------------------------------------------------


@pytest.mark.django_db
def test_deleting_a_good_for_good_keeps_what_is_owed(till, shop, finance_client):
    from common import record_delete

    pid = sell(till, shop, (shop.tea, 2))
    ask_delete(till, pid)
    count_lines(finance_client, shop, (shop.tea, 8, 8))
    with transaction.atomic():
        record_delete.plan_good(shop.tea).run()
    assert not Purchase.objects.filter(pk=pid).exists()
    assert cash.owed(shop.seller.pk) == Decimal("5.00")


@pytest.mark.django_db
def test_deleting_a_store_or_user_for_good(till, shop, finance_client, make_user):
    from common import record_delete

    pid = sell(till, shop, (shop.tea, 2))
    ask_delete(till, pid)
    made = handover(finance_client, shop, "5.00").json()
    finance_client.post(
        f"{ENTRIES}{made['id']}/reverse/", {"reason": "x"}, HTTP_IDEMPOTENCY_KEY=key()
    )
    body = count_lines(finance_client, shop, (shop.tea, 8, 7)).json()
    finance_client.post(f"{COUNTS}{body['id']}/resolve/", {"outcome": "CHARGED", "note": "x"})
    fin = CashEntry.objects.get(kind="MISSING").actor
    root = make_user(Roles.ADMIN, username="root2")
    with transaction.atomic():
        record_delete.plan_user(fin, root).run()
    assert CashEntry.objects.filter(actor__isnull=True).count() == 3
    assert_books()
    with transaction.atomic():
        plan = record_delete.plan_seller(shop.seller)
        assert plan.deletes["cash_entries"] == 4
        plan.run()
    assert not Seller.objects.exists() and not CashEntry.objects.exists()


# --- At the same moment -------------------------------------------------------------------


def _together(*jobs):
    results, barrier = [], threading.Barrier(len(jobs))

    def run(job):
        try:
            barrier.wait()
            results.append(job())
        except Exception as exc:  # noqa: BLE001
            results.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(j,)) for j in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


@pytest.mark.django_db(transaction=True)
def test_concurrent_sales_and_handovers_add_up(shop, make_user):
    from apps.accounts.rbac import sync_rbac

    sync_rbac()
    fin = make_user(username="conc")
    drafts = []
    for _n in range(6):
        p = services.create_purchase(actor=shop.seller_user, service_position=shop.position)
        services.add_item(purchase=p, good=shop.tea, quantity=1)
        drafts.append(p)
    jobs = [
        lambda p=p: services.confirm_cash_sale(
            actor=shop.seller_user, purchase=p, idempotency_key=str(uuid.uuid4())
        )
        for p in drafts
    ] + [
        lambda: cash.hand_over(
            actor=fin, seller=shop.seller, amount="1", note="n", idempotency_key=str(uuid.uuid4())
        )
        for _n in range(3)
    ]
    results = _together(*jobs)
    assert not [r for r in results if isinstance(r, Exception)], results
    assert cash.owed(shop.seller.pk) == Decimal("12.00") and stock(shop.tea) == 4
    assert_books()


@pytest.mark.django_db(transaction=True)
def test_two_approvals_delete_once(shop, make_user):
    p = services.create_purchase(actor=shop.seller_user, service_position=shop.position)
    services.add_item(purchase=p, good=shop.tea, quantity=2)
    services.confirm_cash_sale(actor=shop.seller_user, purchase=p, idempotency_key=key())
    wish = cash.request_deletion(actor=shop.seller_user, purchase=p, reason="x")
    one, two = make_user(username="f1"), make_user(username="f2")
    results = _together(
        lambda: cash.approve(actor=one, request=wish),
        lambda: cash.approve(actor=two, request=wish),
    )
    assert len([r for r in results if isinstance(r, Exception)]) == 1
    assert stock(shop.tea) == 10 and cash.owed(shop.seller.pk) == 0
    assert_books()
