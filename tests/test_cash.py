"""Cash sales at stores that accept cash. The seller keeps the cash; finance pays the store its
card sales."""

import io
import threading
import uuid
from decimal import Decimal

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from openpyxl import load_workbook
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserRole
from apps.accounts.rbac import ALL, ROLES, Roles
from apps.audit.models import AuditLog
from apps.cash import services as cash
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
IMPORT_GOODS = "/api/v1/imports/goods/"


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


def delete_sale(client, pid, reason="Wrong goods"):
    return client.post(f"{PURCHASES}{pid}/delete-cash/", {"reason": reason})


def code(response):
    return response.status_code, response.json()["error"]["code"]


def assert_books():
    assert finance.ledger_mismatches() == []
    assert goods.stock_mismatches() == []


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
    assert AuditLog.objects.filter(action="purchase.paid_cash").count() == 1
    assert_books()


@pytest.mark.django_db
def test_a_store_without_cash_sells_by_card_only(till, world):
    pid = new_purchase(till, world, (world.tea, 1))
    assert code(pay_cash(till, pid)) == (409, "CASH_NOT_ACCEPTED")
    assert stock(world.tea) == 10
    assert test_purchases.confirm(till, pid).status_code == 201


@pytest.mark.django_db
def test_a_retry_or_second_click_sells_once(till, shop):
    pid = new_purchase(till, shop, (shop.tea, 1))
    idem = key()
    assert pay_cash(till, pid, idem=idem).status_code == 201
    assert pay_cash(till, pid, idem=idem).status_code == 200
    assert code(pay_cash(till, pid)) == (409, "PURCHASE_NOT_DRAFT")
    assert stock(shop.tea) == 9
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
    assert delete_sale(client, pid).status_code == 403
    # The store's whole cash is not theirs to see.
    assert client.get(STORES).status_code == 403


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


# --- Deleting a cash sale -----------------------------------------------------------------


@pytest.mark.django_db
def test_a_cash_sale_is_neither_changed_nor_deleted_as_a_wallet_sale(till, shop, admin_client):
    pid = sell(till, shop, (shop.tea, 2))
    for client in (till, admin_client):
        assert code(client.delete(f"{PURCHASES}{pid}/")) == (409, "CASH_DELETED_BY_FINANCE")
        change = {"items": [{"good": shop.tea.pk, "quantity": 1}]}
        response = client.patch(f"{PURCHASES}{pid}/", change, format="json")
        assert code(response) == (409, "CASH_SALE_FIXED")
    assert stock(shop.tea) == 8


@pytest.mark.django_db
def test_finance_deletes_a_cash_sale(till, shop, finance_client):
    pid = sell(till, shop, (shop.tea, 2))
    assert delete_sale(till, pid).status_code == 403
    assert delete_sale(finance_client, pid, " ").status_code == 400  # say why
    body = delete_sale(finance_client, pid).json()
    assert (body["status"], body["refund_reason"]) == ("REFUNDED", "Wrong goods")
    purchase = Purchase.objects.get(pk=pid)
    assert purchase.refunded_by.username == "fin"
    assert stock(shop.tea) == 10
    assert code(delete_sale(finance_client, pid)) == (409, "PURCHASE_NOT_PAID")
    assert AuditLog.objects.filter(action="purchase.refunded").count() == 1
    assert_books()


@pytest.mark.django_db
def test_only_finance_and_admins_delete_cash_sales(till, shop, make_user, admin_client):
    pid = sell(till, shop, (shop.tea, 1))
    for role in (Roles.BOSS, Roles.MANAGER, Roles.BUILDING_OWNER, Roles.BUILDING_MANAGER):
        assert delete_sale(client_of(make_user(role)), pid).status_code in (403, 404)
    assert Purchase.objects.get(pk=pid).status == "CONFIRMED"
    assert delete_sale(admin_client, pid).status_code == 200


@pytest.mark.django_db
def test_not_ones_own_sale_or_store(till, shop, finance_client, admin_client):
    own = sell(admin_client, shop, (shop.tea, 1))
    assert code(delete_sale(admin_client, own)) == (403, "OWN_SALE")
    assert delete_sale(finance_client, own).status_code == 200
    pid = sell(admin_client, shop, (shop.tea, 1))
    UserRole.objects.create(
        user=shop.seller_user, role=Role.objects.get(code=Roles.FINANCE_MANAGER)
    )
    fresh = client_of(type(shop.seller_user).objects.get(pk=shop.seller_user.pk))
    assert code(delete_sale(fresh, pid)) == (403, "OWN_STORE")


@pytest.mark.django_db
def test_wallet_sales_and_drafts_are_not_deleted_this_way(till, shop, finance_client):
    draft = new_purchase(till, shop, (shop.tea, 1))
    assert code(delete_sale(finance_client, draft)) == (409, "CASH_SALE_FIXED")
    test_purchases.confirm(till, draft)
    assert code(delete_sale(finance_client, draft)) == (409, "CASH_SALE_FIXED")
    assert till.delete(f"{PURCHASES}{draft}/").status_code == 200  # as before


@pytest.mark.django_db
def test_the_permission_is_renamed_for_every_role_that_had_it():
    import importlib

    from django.apps import apps as django_apps

    from apps.accounts.models import Permission

    Permission.objects.filter(codename="cash.delete").delete()
    old = Permission.objects.create(codename="cash.approve")
    role = Role.objects.create(code="CUSTOM", name="Custom")
    role.permissions.add(old)
    module = importlib.import_module("apps.accounts.migrations.0013_cash_delete")
    module.rename("cash.approve", "cash.delete")(django_apps, None)
    assert list(role.permissions.values_list("codename", flat=True)) == ["cash.delete"]


# --- Prices and goods ---------------------------------------------------------------------


def price(good):
    return Good.all_objects.get(pk=good.pk).price


def changes(client, shop, **query):
    return client.get(f"{STORES}{shop.seller.pk}/price-changes/", query).json()


@pytest.mark.django_db
def test_finance_sees_every_price_change(till, shop, finance_client, admin_client):
    assert till.patch(f"{GOODS}{shop.tea.pk}/", {"price": "0.50"}).json()["price"] == "0.50"
    till.patch(f"{GOODS}{shop.tea.pk}/", {"price": "0.50", "description": "x"})  # no change
    till.patch(f"{GOODS}{shop.tea.pk}/", {"price": "2.50"})
    admin_client.patch(f"{GOODS}{shop.cake.pk}/", {"price": "5"})
    rows = changes(finance_client, shop)
    assert [(r["good_name"], r["old_price"], r["new_price"], r["by"]) for r in rows] == [
        ("Cake", "4.00", "5.00", "root"),
        ("Tea", "0.50", "2.50", "cafe"),
        ("Tea", "2.50", "0.50", "cafe"),
    ]
    assert [r["by_store"] for r in rows] == [False, True, True]
    store = finance_client.get(STORES).json()["stores"][0]
    assert store["price_changes"] == 3
    assert [r["id"] for r in changes(till, shop)] == [r["id"] for r in rows]  # their own
    assert changes(finance_client, shop, date_from="2020-01-01", date_to="2020-01-02") == []


@pytest.mark.django_db
def test_a_new_good_is_on_sale_at_once_and_listed(till, shop, finance_client):
    made = till.post(
        GOODS, {"service_position": shop.position.pk, "name": "Candy", "price": "0.10"}
    ).json()
    assert made["is_active"] is True
    sell(
        till,
        shop,
        (
            Good.objects.create(
                service_position=shop.position, name="Gum", price="1", track_stock=False
            ),
            1,
        ),
    )
    (row,) = changes(finance_client, shop)
    assert (row["good_name"], row["old_price"], row["new_price"]) == ("Candy", None, "0.10")
    assert till.delete(f"{GOODS}{made['id']}/").status_code == 204
    assert len(changes(finance_client, shop)) == 1  # a removed good stays listed


@pytest.mark.django_db
def test_price_changes_are_per_store(till, shop, make_user, finance_client):
    till.patch(f"{GOODS}{shop.tea.pk}/", {"price": "3"})
    other = make_user(Roles.GOODS_SELLER, username="other")
    Seller.objects.create(name="Other", user=other, accepts_cash=True)
    url = f"{STORES}{shop.seller.pk}/price-changes/"
    assert client_of(other).get(url).status_code == 404
    assert client_of(make_user(Roles.MANAGER)).get(url).status_code == 404
    counts = {s["name"]: s["price_changes"] for s in finance_client.get(STORES).json()["stores"]}
    assert counts == {"Cafe": 1, "Other": 0}


@pytest.mark.django_db
def test_the_store_works_with_its_stock_as_usual(till, shop):
    url = f"{GOODS}{shop.tea.pk}/stock/"
    damage = {"kind": "DAMAGE", "quantity": 1, "reason": "Broken"}
    count = {"kind": "ADJUSTMENT", "counted_quantity": 3, "reason": "Count"}
    assert till.post(url, damage).status_code == 201
    assert till.post(url, count).status_code == 201
    assert stock(shop.tea) == 3


@pytest.mark.django_db
def test_courts_are_not_listed(shop, finance_client):
    court = Good.objects.create(
        service_position=shop.position,
        name="Court",
        price="5",
        kind=GoodKind.RENTAL,
        track_stock=False,
    )
    goods.update_good(actor=shop.seller_user, good=court, price=Decimal("6"))
    assert changes(finance_client, shop) == []


# --- What finance and the store see -------------------------------------------------------


@pytest.mark.django_db
def test_card_sales_are_paid_to_the_store_and_cash_is_kept(till, shop, finance_client):
    sell(till, shop, (shop.tea, 2))  # 5.00 in cash
    gone = sell(till, shop, (shop.tea, 1))
    delete_sale(finance_client, gone)
    for good, n in ((shop.cake, 1), (shop.coffee, 2)):  # 4.00 and 6.00 by card
        assert test_purchases.confirm(till, new_purchase(till, shop, (good, n))).status_code == 201
    new_purchase(till, shop, (shop.tea, 1))  # not paid yet
    row = finance_client.get(STORES).json()["stores"][0]
    assert (row["card_total"], row["card_count"]) == ("10.00", 2)
    assert (row["cash_total"], row["cash_count"], row["total"]) == ("5.00", 1, "15.00")
    body = finance_client.get(f"{STORES}{shop.seller.pk}/").json()
    assert [(d["card_total"], d["cash_total"], d["total"]) for d in body["daily"]] == [
        ("10.00", "5.00", "15.00")
    ]
    # Refunded card sales are not paid.
    card = Purchase.objects.filter(payment="WALLET", status="CONFIRMED", total="6.00").get()
    assert till.delete(f"{PURCHASES}{card.pk}/").status_code == 200
    assert finance_client.get(STORES).json()["stores"][0]["card_total"] == "4.00"
    past = {"date_from": "2020-01-01", "date_to": "2020-01-02"}
    row = finance_client.get(STORES, past).json()["stores"][0]
    assert (row["card_total"], row["total"]) == ("0.00", "0.00")


@pytest.mark.django_db
def test_stores_see_their_own_cash_only(till, shop, make_user, finance_client):
    sell(till, shop, (shop.tea, 1))
    other = make_user(Roles.GOODS_SELLER, username="other")
    Seller.objects.create(name="Other", user=other, accepts_cash=True)
    assert [s["name"] for s in till.get(STORES).json()["stores"]] == ["Cafe"]
    theirs = client_of(other)
    assert [s["name"] for s in theirs.get(STORES).json()["stores"]] == ["Other"]
    assert theirs.get(f"{STORES}{shop.seller.pk}/").status_code == 404
    names = {s["name"] for s in finance_client.get(STORES).json()["stores"]}
    assert names == {"Cafe", "Other"}
    boss = client_of(make_user(Roles.BOSS))
    assert boss.get(STORES).status_code == 200
    for role in (Roles.MANAGER, Roles.DEVELOPER, Roles.BUILDING_OWNER, Roles.PLAYGROUND_SELLER):
        assert client_of(make_user(role)).get(STORES).status_code == 403
    assert APIClient().get(STORES).status_code == 401


@pytest.mark.django_db
def test_every_open_store_is_listed_and_closed_ones_with_sales(till, shop, finance_client):
    Seller.objects.create(name="Open")
    closed = Seller.objects.create(name="Closed", status="CLOSED")
    test_purchases.confirm(till, new_purchase(till, shop, (shop.tea, 1)))
    Seller.objects.filter(pk=shop.seller.pk).update(status="CLOSED", accepts_cash=False)
    names = [s["name"] for s in finance_client.get(STORES).json()["stores"]]
    assert names == ["Cafe", "Open"]
    assert finance_client.get(f"{STORES}{closed.pk}/").status_code == 404


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
    assert {"cash.view", "cash.delete"} <= fin and "cash.handover" not in ALL
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
    assert len(book.worksheets) == 3
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
def test_concurrent_sales_of_the_last_goods(shop):
    drafts = []
    for _n in range(3):
        p = services.create_purchase(actor=shop.seller_user, service_position=shop.position)
        services.add_item(purchase=p, good=shop.cake, quantity=1)
        drafts.append(p)
    results = _together(
        *[
            lambda p=p: services.confirm_cash_sale(
                actor=shop.seller_user, purchase=p, idempotency_key=str(uuid.uuid4())
            )
            for p in drafts
        ]
    )
    assert len([r for r in results if isinstance(r, Exception)]) == 1
    assert stock(shop.cake) == 0
    assert_books()


@pytest.mark.django_db(transaction=True)
def test_two_deletes_at_once_delete_once(shop, make_user):
    p = services.create_purchase(actor=shop.seller_user, service_position=shop.position)
    services.add_item(purchase=p, good=shop.tea, quantity=2)
    p, _new = services.confirm_cash_sale(actor=shop.seller_user, purchase=p, idempotency_key=key())
    one, two = make_user(username="f1"), make_user(username="f2")
    results = _together(
        lambda: cash.delete_sale(actor=one, purchase=p, reason="x"),
        lambda: cash.delete_sale(actor=two, purchase=p, reason="x"),
    )
    assert len([r for r in results if isinstance(r, Exception)]) == 1
    assert stock(shop.tea) == 10
    assert_books()
