"""A store's categories of goods: each store manages its own; staff manage every store's.
A good's category must be one of its own store's; removing a category keeps the goods."""

import pytest

from apps.accounts.rbac import Roles
from apps.goods.models import Good, GoodCategory
from apps.sellers.models import Seller, ServicePosition
from tests.test_goods import make_good, media_root, position  # noqa: F401

CATEGORIES = "/api/v1/good-categories/"
GOODS = "/api/v1/goods/"


@pytest.fixture
def store_client(auth_client, make_user, position):  # noqa: F811
    user = make_user(Roles.GOODS_SELLER)
    Seller.objects.filter(pk=position.seller_id).update(user=user)
    return auth_client(user)


@pytest.fixture
def other_store(db):
    return ServicePosition.objects.create(seller=Seller.objects.create(name="Shop"), name="Desk")


@pytest.mark.django_db
def test_a_store_manages_its_own_categories(store_client, position, other_store, make_good):  # noqa: F811
    r = store_client.post(CATEGORIES, {"name": "Drinks"})
    assert r.status_code == 201, r.json()
    drinks = r.json()
    assert (drinks["seller"], drinks["position"]) == (position.seller_id, 0)
    assert store_client.post(CATEGORIES, {"name": " drinks "}).status_code == 400  # taken
    food = store_client.post(CATEGORIES, {"name": "Food"}).json()
    assert food["position"] == 1
    # Another store's category is not visible.
    GoodCategory.objects.create(seller=other_store.seller, name="Phones")
    assert [c["name"] for c in store_client.get(CATEGORIES).json()] == ["Drinks", "Food"]

    tea = make_good(name="Tea")
    r = store_client.patch(f"{GOODS}{tea.pk}/", {"category": drinks["id"]}, format="json")
    assert r.status_code == 200, r.json()
    assert r.json()["category_name"] == "Drinks"
    assert [c["goods_count"] for c in store_client.get(CATEGORIES).json()] == [1, 0]
    listed = store_client.get(GOODS, {"category": drinks["id"]}).json()["results"]
    assert [g["name"] for g in listed] == ["Tea"]

    assert (
        store_client.patch(f"{CATEGORIES}{drinks['id']}/", {"name": "Hot drinks"}).status_code
        == 200
    )
    assert store_client.delete(f"{CATEGORIES}{drinks['id']}/").status_code == 204
    tea.refresh_from_db()
    assert tea.category_id is None and Good.objects.filter(pk=tea.pk).exists()


@pytest.mark.django_db
def test_a_good_takes_only_its_own_stores_category(auth_client, make_user, make_good, other_store):  # noqa: F811
    staff = auth_client(make_user(Roles.ADMIN))
    phones = GoodCategory.objects.create(seller=other_store.seller, name="Phones")
    tea = make_good(name="Tea")
    r = staff.patch(f"{GOODS}{tea.pk}/", {"category": phones.pk}, format="json")
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    # Staff see every store's and add to any store.
    r = staff.post(CATEGORIES, {"name": "Snacks", "seller": tea.service_position.seller_id})
    assert r.status_code == 201, r.json()
    assert {c["name"] for c in staff.get(CATEGORIES).json()} == {"Phones", "Snacks"}
    assert staff.post(CATEGORIES, {"name": "No store"}).status_code == 400


@pytest.mark.django_db
def test_developers_cannot_manage_categories(auth_client, make_user):
    developer = auth_client(make_user(Roles.DEVELOPER))
    assert developer.get(CATEGORIES).status_code == 403
    assert developer.post(CATEGORIES, {"name": "X"}).status_code == 403


@pytest.mark.django_db
def test_stock_movement_summary_for_a_range(auth_client, make_user, make_good):  # noqa: F811
    from datetime import timedelta

    from django.utils import timezone

    from apps.goods import services

    staff = auth_client(make_user(Roles.ADMIN))
    tea = make_good(name="Tea", quantity=10, track_stock=True)  # before the range
    make_good(name="Idle", quantity=3, track_stock=True)  # moves only before the range
    start = timezone.now()
    services.move_stock(good=tea, kind="RESTOCK", delta=5)
    services.move_stock(good=tea, kind="SALE", delta=-4)
    services.move_stock(good=tea, kind="DAMAGE", delta=-1, reason="Broken")

    rows = staff.get(
        "/api/v1/inventory/movements/summary/",
        {
            "created_after": start.isoformat(),
            "created_before": (start + timedelta(days=1)).isoformat(),
        },
    ).json()
    assert [
        (r["name"], r["start"], r["added"], r["sold"], r["damaged"], r["end"]) for r in rows
    ] == [("Tea", 10, 5, 4, 1, 10)]
    assert rows[0]["stock_now"] == 10
    bad = staff.get("/api/v1/inventory/movements/summary/").json()
    assert bad["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.django_db
def test_sales_report_excel(auth_client, make_user, make_good):  # noqa: F811
    from io import BytesIO

    from openpyxl import load_workbook

    staff = auth_client(make_user(Roles.ADMIN))
    make_good(name="Tea")
    r = staff.get(
        "/api/v1/goods/statistics/export/", {"date_from": "2026-10-01", "date_to": "2026-10-17"}
    )
    assert r.status_code == 200
    assert 'filename="sales-2026-10-01-to-2026-10-17.xlsx"' in r["Content-Disposition"]
    book = load_workbook(BytesIO(r.content))
    assert book.sheetnames == ["Summary", "Goods sold"]
    assert book["Summary"]["A4"].value == "Money taken"
