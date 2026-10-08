"""GET /goods/statistics/: per good, paid till sales in a period and the stock now."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import transaction
from django.utils import timezone

from apps.accounts.rbac import Roles
from apps.developers.models import Developer
from apps.finance import services as finance
from apps.finance.models import TransactionKind
from apps.goods.models import Good, GoodKind
from apps.purchases.models import Purchase, PurchaseItem, PurchaseKind, PurchaseStatus
from apps.sellers.models import Seller, ServicePosition

STATS = "/api/v1/goods/statistics/"


@pytest.fixture
def cafe(db, make_user):
    class Cafe:
        pass

    c = Cafe()
    c.user = make_user(Roles.GOODS_SELLER, username="cafe")
    c.seller = Seller.objects.create(name="Cafe", user=c.user)
    c.counter = ServicePosition.objects.create(seller=c.seller, name="Counter")
    c.tea = Good.objects.create(
        service_position=c.counter,
        name="Tea",
        price="2.00",
        kind=GoodKind.SERVICE,
        track_stock=False,
    )
    c.cake = Good.objects.create(service_position=c.counter, name="Cake", price="4.00", quantity=7)
    c.court = Good.objects.create(
        service_position=c.counter,
        name="Court",
        price="9.00",
        kind=GoodKind.RENTAL,
        track_stock=False,
    )
    c.developer = Developer.objects.create(employee_number="E1", full_name="Ada")
    finance.deposit(actor=None, developer=c.developer, amount="500.00", idempotency_key="seed-0001")
    other = Seller.objects.create(name="Other")
    c.foreign = Good.objects.create(
        service_position=ServicePosition.objects.create(seller=other, name="Elsewhere"),
        name="Juice",
        price="3.00",
        track_stock=False,
    )
    return c


def sale(cafe, lines, *, status=PurchaseStatus.CONFIRMED, when=None, kind=PurchaseKind.SALE):
    """A purchase of `lines` [(good, quantity)], paid `when` (default: now) by Ada."""
    total = sum(Decimal(good.price) * quantity for good, quantity in lines)
    paid = {}
    if status == PurchaseStatus.CONFIRMED:
        with transaction.atomic():
            paid["account_transaction"] = finance.post_transaction(
                account=finance.open_account(cafe.developer),
                kind=TransactionKind.PURCHASE,
                amount=-total,
                actor=None,
                description="t",
                reference="t",
            )
        paid |= {
            "developer": cafe.developer,
            "total": total,
            "confirmed_at": when or timezone.now(),
        }
    position = lines[0][0].service_position
    purchase = Purchase.objects.create(
        seller=position.seller, service_position=position, kind=kind, status=status, **paid
    )
    for good, quantity in lines:
        PurchaseItem.objects.create(
            purchase=purchase,
            good=good,
            quantity=quantity,
            unit_price=good.price,
            line_total=Decimal(good.price) * quantity,
        )
    return purchase


def by_name(response):
    assert response.status_code == 200, response.content
    return {row["name"]: row for row in response.json()}


def test_a_goods_seller_sees_its_own_goods_sales(auth_client, cafe):
    sale(cafe, [(cafe.tea, 2), (cafe.cake, 1)])
    sale(cafe, [(cafe.tea, 3)])
    sale(cafe, [(cafe.tea, 5)], status=PurchaseStatus.DRAFT)  # not paid: not counted
    sale(cafe, [(cafe.tea, 9)], status=PurchaseStatus.CANCELLED)
    rows = by_name(auth_client(cafe.user).get(STATS))
    assert set(rows) == {"Tea", "Cake"}  # no courts, no other store's goods
    tea, cake = rows["Tea"], rows["Cake"]
    assert (tea["sold"], tea["sales"], tea["money"], tea["stock"]) == (5, 2, "10.00", None)
    assert (cake["sold"], cake["sales"], cake["money"], cake["stock"]) == (1, 1, "4.00", 7)
    assert list(rows) == ["Tea", "Cake"]  # most money first


def test_the_period_counts_sales_paid_in_it(auth_client, cafe):
    now = timezone.now()
    sale(cafe, [(cafe.tea, 1)], when=now - timedelta(days=3))
    sale(cafe, [(cafe.tea, 4)], when=now)
    after = (now - timedelta(hours=1)).isoformat()
    rows = by_name(auth_client(cafe.user).get(STATS, {"confirmed_after": after}))
    assert rows["Tea"]["sold"] == 4
    assert rows["Cake"]["sold"] == 0  # goods without sales are listed too


def test_who_may_see_goods_statistics(auth_client, cafe, make_user):
    sale(cafe, [(cafe.foreign, 1)])
    assert auth_client(make_user(Roles.DEVELOPER)).get(STATS).status_code == 403
    # Staff need both good.view and purchase.view: MANAGER has only the first.
    assert auth_client(make_user(Roles.MANAGER)).get(STATS).status_code == 403
    staff = by_name(auth_client(make_user(Roles.BOSS)).get(STATS))
    assert {"Tea", "Cake", "Juice"} <= set(staff) and "Court" not in staff
    playground = make_user(Roles.PLAYGROUND_SELLER)
    cafe.seller.user = playground
    cafe.seller.save(update_fields=["user"])
    assert auth_client(playground).get(STATS).json() == []  # courts are not goods
