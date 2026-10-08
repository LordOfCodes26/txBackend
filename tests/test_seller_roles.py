"""The two seller roles: PLAYGROUND_SELLER (courts, bookings, the playground desk, booking
payments) and GOODS_SELLER (goods, stock, the till and its sales), each for its own store."""

import importlib
from datetime import time

import pytest
from django.apps import apps as django_apps

from apps.accounts.models import Role, UserRole
from apps.accounts.rbac import SELLER_ROLES, Roles
from apps.goods.models import Good, GoodKind, RentalSettings
from apps.purchases.models import Purchase, PurchaseKind
from apps.sellers.models import Seller, ServicePosition

GOODS = "/api/v1/goods/"
INVENTORY = "/api/v1/inventory/movements/"
PURCHASES = "/api/v1/purchases/"
BOOKINGS = "/api/v1/bookings/"
CHECKOUT = "/api/v1/bookings/checkout/"

PLAYGROUND_ONLY = (Roles.PLAYGROUND_SELLER,)
RULES = {"slot_minutes": 60, "opening_time": "08:00", "closing_time": "20:00"}
GOODS_ONLY = (Roles.GOODS_SELLER,)


@pytest.fixture
def store(db, make_user):
    """One store doing both: a product, a court, a till sale and a booking payment. The
    login's roles are set per test with `store.login(*roles)`."""

    class Store:
        pass

    s = Store()
    s.user = make_user(username="shop")
    s.seller = Seller.objects.create(name="Sports and snacks", user=s.user)
    s.position = ServicePosition.objects.create(seller=s.seller, name="Front desk")
    s.product = Good.objects.create(
        service_position=s.position, name="Water", price="1.00", track_stock=False
    )
    s.court = Good.objects.create(
        service_position=s.position,
        name="Court 1",
        price="20.00",
        kind=GoodKind.RENTAL,
        track_stock=False,
    )
    RentalSettings.objects.create(
        good=s.court, slot_minutes=60, opening_time=time(8), closing_time=time(20)
    )
    s.sale = Purchase.objects.create(seller=s.seller, service_position=s.position)
    s.booking = Purchase.objects.create(
        seller=s.seller, service_position=s.position, kind=PurchaseKind.BOOKING
    )

    def login(*roles):
        for code in roles:
            UserRole.objects.create(user=s.user, role=Role.objects.get(code=code))
        return s.user

    s.login = login
    return s


def ids(response):
    assert response.status_code == 200, response.content
    return {row["id"] for row in response.json()["results"]}


@pytest.mark.parametrize(
    ("roles", "sees"),
    [(GOODS_ONLY, {"product"}), (PLAYGROUND_ONLY, {"court"}), (SELLER_ROLES, {"product", "court"})],
)
def test_each_role_sees_its_part_of_the_goods(auth_client, store, roles, sees):
    client = auth_client(store.login(*roles))
    assert ids(client.get(GOODS)) == {getattr(store, name).pk for name in sees}


@pytest.mark.parametrize(
    ("roles", "sees"),
    [(GOODS_ONLY, {"sale"}), (PLAYGROUND_ONLY, {"booking"}), (SELLER_ROLES, {"sale", "booking"})],
)
def test_each_role_sees_its_part_of_the_purchases(auth_client, store, roles, sees):
    client = auth_client(store.login(*roles))
    assert ids(client.get(PURCHASES)) == {getattr(store, name).pk for name in sees}


def test_goods_seller_has_no_playground(auth_client, store):
    client = auth_client(store.login(*GOODS_ONLY))
    assert client.get(BOOKINGS).status_code == 403
    assert client.post(CHECKOUT, {"good": store.court.pk}).status_code == 403
    court = {"service_position": store.position.pk, "name": "Court 2", "price": "5.00"}
    court |= {"kind": "RENTAL", "track_stock": False, "rental": RULES}
    assert client.post(GOODS, court, format="json").status_code == 403
    assert client.get(f"{GOODS}{store.court.pk}/").status_code == 404
    # Goods work as before.
    assert client.get(INVENTORY).status_code == 200
    water = {"service_position": store.position.pk, "name": "Juice", "price": "2.00"}
    assert client.post(GOODS, water | {"track_stock": False}).status_code == 201
    assert client.post(PURCHASES, {"service_position": store.position.pk}).status_code == 201


def test_playground_seller_has_no_goods(auth_client, store):
    client = auth_client(store.login(*PLAYGROUND_ONLY))
    assert client.get(INVENTORY).status_code == 403
    juice = {"service_position": store.position.pk, "name": "Juice", "price": "2.00"}
    assert client.post(GOODS, juice | {"track_stock": False}).status_code == 403
    assert client.get(f"{GOODS}{store.product.pk}/").status_code == 404
    # No till purchases: the playground desk takes booking payments.
    assert client.post(PURCHASES, {"service_position": store.position.pk}).status_code == 403
    assert client.get(f"{PURCHASES}{store.sale.pk}/").status_code == 404
    # The playground works, including adding a court.
    assert client.get(BOOKINGS).status_code == 200
    court = {"service_position": store.position.pk, "name": "Court 2", "price": "5.00"}
    court |= {"kind": "RENTAL", "track_stock": False, "rental": RULES}
    assert client.post(GOODS, court, format="json").status_code == 201
    assert client.get(f"{GOODS}{store.court.pk}/").status_code == 200
    assert client.get(f"{PURCHASES}{store.booking.pk}/").status_code == 200


def test_either_role_runs_the_store(auth_client, store, make_user):
    """The store itself (and its counters) belongs to both parts."""
    for roles in (GOODS_ONLY, PLAYGROUND_ONLY):
        user = make_user(*roles)
        store.seller.user = user
        store.seller.save(update_fields=["user"])
        mine = auth_client(user).get("/api/v1/sellers/me/")
        assert mine.status_code == 200 and mine.json()["id"] == store.seller.pk


def test_users_can_be_listed_by_several_roles(auth_client, admin, make_user):
    goods = make_user(*GOODS_ONLY)
    playground = make_user(*PLAYGROUND_ONLY)
    both = make_user(*SELLER_ROLES)
    make_user(Roles.DEVELOPER)
    rows = auth_client(admin).get("/api/v1/users/?role=GOODS_SELLER,PLAYGROUND_SELLER")
    assert ids(rows) == {goods.pk, playground.pk, both.pk}


@pytest.mark.django_db
def test_migration_gives_every_seller_both_roles(make_user):
    """0008: SELLER became the two roles; its users keep everything they had."""
    migration = importlib.import_module("apps.accounts.migrations.0008_split_seller_role")
    old = Role.objects.create(code="SELLER", name="Seller", is_system=True)
    seller = make_user()
    UserRole.objects.create(user=seller, role=old)
    other = make_user(Roles.DEVELOPER)

    migration.split_seller(django_apps, None)

    assert not Role.objects.filter(code="SELLER").exists()
    assert set(seller.roles.values_list("code", flat=True)) == set(SELLER_ROLES)
    assert set(other.roles.values_list("code", flat=True)) == {Roles.DEVELOPER}
