"""Position managers: one login per sell position, limited to that position's data."""

import pytest
from asgiref.sync import async_to_sync

from apps.accounts.rbac import Roles
from apps.goods.models import Good
from apps.realtime.consumers import _authorize
from apps.realtime.tickets import issue_ticket
from apps.rfid.models import Building
from apps.sellers.models import Seller, ServicePosition

pytestmark = pytest.mark.django_db

GOODS = "/api/v1/goods/"
POSITIONS = "/api/v1/service-positions/"
PURCHASES = "/api/v1/purchases/"
PAYOUTS = "/api/v1/seller-finance/payouts/"


@pytest.fixture
def cafe(make_user):
    owner = make_user(Roles.SELLER, email="owner@cafe.x")
    seller = Seller.objects.create(name="Cafe", user=owner)
    manager = make_user(email="kiosk@cafe.x")
    counter = ServicePosition.objects.create(seller=seller, name="Counter")
    kiosk = ServicePosition.objects.create(seller=seller, name="Kiosk", manager=manager)
    tea = Good.objects.create(service_position=counter, name="Tea", price="2.00")
    juice = Good.objects.create(service_position=kiosk, name="Juice", price="3.00")

    class C:
        pass

    c = C()
    c.__dict__.update(locals())
    return c


def names(response):
    return sorted(row["name"] for row in response.json()["results"])


def test_manager_sees_and_manages_only_their_positions_goods(auth_client, cafe):
    client = auth_client(cafe.manager)
    assert names(client.get(GOODS)) == ["Juice"]
    assert client.get(f"{GOODS}{cafe.tea.pk}/").status_code == 404
    assert client.patch(f"{GOODS}{cafe.tea.pk}/", {"price": "1.00"}).status_code == 404
    assert client.patch(f"{GOODS}{cafe.juice.pk}/", {"price": "3.50"}).status_code == 200

    other = client.post(
        GOODS, {"service_position": cafe.counter.pk, "name": "Cake", "price": "4.00"}
    )
    assert "service_position" in other.json()["error"]["details"]
    own = client.post(GOODS, {"service_position": cafe.kiosk.pk, "name": "Cake", "price": "4.00"})
    assert own.status_code == 201, own.json()


def test_owner_still_sees_every_position(auth_client, cafe):
    assert names(auth_client(cafe.owner).get(GOODS)) == ["Juice", "Tea"]
    assert names(auth_client(cafe.owner).get(POSITIONS)) == ["Counter", "Kiosk"]


def test_manager_sells_only_at_their_position(auth_client, cafe):
    client = auth_client(cafe.manager)
    r = client.post(PURCHASES, {"service_position": cafe.counter.pk})
    assert "service_position" in r.json()["error"]["details"]
    own = client.post(PURCHASES, {"service_position": cafe.kiosk.pk})
    assert own.status_code == 201
    counter_sale = auth_client(cafe.owner).post(PURCHASES, {"service_position": cafe.counter.pk})
    client = auth_client(cafe.manager)
    assert [p["id"] for p in client.get(PURCHASES).json()["results"]] == [own.json()["id"]]
    assert client.get(f"{PURCHASES}{counter_sale.json()['id']}/").status_code == 404


def test_manager_edits_own_position_but_not_its_setup(auth_client, cafe):
    client = auth_client(cafe.manager)
    assert names(client.get(POSITIONS)) == ["Kiosk"]
    assert client.patch(f"{POSITIONS}{cafe.kiosk.pk}/", {"location": "Hall"}).status_code == 200
    r = client.patch(f"{POSITIONS}{cafe.kiosk.pk}/", {"manager": None}, format="json")
    assert "manager" in r.json()["error"]["details"]
    assert client.post(POSITIONS, {"name": "New"}).status_code == 403
    assert client.delete(f"{POSITIONS}{cafe.kiosk.pk}/").status_code == 403
    assert client.get(f"{POSITIONS}{cafe.counter.pk}/").status_code == 404


def test_manager_has_no_access_to_seller_money(auth_client, cafe):
    client = auth_client(cafe.manager)
    assert client.get(PAYOUTS).status_code == 403
    assert client.post(PAYOUTS, {"amount": "1.00"}).status_code == 403
    assert auth_client(cafe.owner).get(PAYOUTS).status_code == 200


def test_staff_assign_manager_and_building(auth_client, make_user, cafe):
    b1 = Building.objects.create(code="B1", name="Building 1")
    staff_user = make_user(Roles.BOSS)
    staff = auth_client(staff_user)
    newbie = make_user(email="counter@cafe.x")
    r = staff.patch(
        f"{POSITIONS}{cafe.counter.pk}/",
        {"manager": newbie.pk, "building": b1.pk},
        format="json",
    )
    assert r.status_code == 200, r.json()
    assert (r.json()["manager_email"], r.json()["building_name"]) == (
        "counter@cafe.x",
        "Building 1",
    )
    assert names(auth_client(newbie).get(GOODS)) == ["Tea"]
    staff = auth_client(staff_user)
    # The building is optional.
    r = staff.patch(f"{POSITIONS}{cafe.counter.pk}/", {"building": None}, format="json")
    assert r.json()["building"] is None
    # A seller's owner can't also be a position manager.
    r = staff.patch(f"{POSITIONS}{cafe.counter.pk}/", {"manager": cafe.owner.pk}, format="json")
    assert "manager" in r.json()["error"]["details"]


def test_owner_assigns_managers_to_own_positions(auth_client, make_user, cafe):
    newbie = make_user(email="counter@cafe.x")
    r = auth_client(cafe.owner).patch(
        f"{POSITIONS}{cafe.counter.pk}/", {"manager": newbie.pk}, format="json"
    )
    assert r.status_code == 200


@pytest.mark.django_db(transaction=True)
def test_manager_follows_only_their_counter_live(cafe):
    def allowed(position):
        return async_to_sync(_authorize)(position.pk, issue_ticket(cafe.manager), "")[0]

    assert allowed(cafe.kiosk) is True
    assert allowed(cafe.counter) is False
