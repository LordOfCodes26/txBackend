"""New store in one step: login, seller, first counter and till reader, all or nothing."""

import pytest

from apps.accounts.models import User
from apps.accounts.rbac import Roles
from apps.audit.models import AuditLog
from apps.rfid import services as rfid
from apps.rfid.models import Building, RFIDDevice
from apps.sellers.models import Seller, ServicePosition

URL = "/api/v1/sellers/onboard/"
PASSWORD = "Counter-Pass-2026"


@pytest.fixture
def admin(auth_client, make_user):
    return auth_client(make_user(Roles.ADMIN, username="root"))


@pytest.fixture
def b1(db):
    return Building.objects.create(code="B1", name="Building 1")


def till(code="Reader9", seller=None):
    device, _key = rfid.register_device(actor=None, code=code, name=code, purpose="TILL")
    if seller:
        device.seller = seller
        device.save()
    return device


def test_store_with_new_login_counter_and_till(admin, b1):
    reader = till()
    r = admin.post(
        URL,
        {
            "name": "Peak Cafe",
            "contact_name": "Kim Chol",
            "phone": "+850 2 123",
            "username": "peakcafe",
            "password": PASSWORD,
            "building": b1.pk,
            "location": "Lobby",
            "till_reader": reader.pk,
        },
        format="json",
    )
    assert r.status_code == 201, r.json()
    body = r.json()
    seller = Seller.objects.get(pk=body["seller"])
    login = User.objects.get(username="peakcafe")
    assert seller.user == login and login.roles.filter(code=Roles.SELLER).exists()
    assert login.check_password(PASSWORD) and login.full_name == "Kim Chol"
    counter = ServicePosition.objects.get(pk=body["position"])
    # The counter is named after the store unless a name is given.
    assert (counter.seller, counter.name, counter.building, counter.location) == (
        seller,
        "Peak Cafe",
        b1,
        "Lobby",
    )
    reader.refresh_from_db()
    assert body["reader"] == reader.pk and reader.seller == seller
    actions = set(AuditLog.objects.values_list("action", flat=True))
    assert {"user.created", "seller.created", "seller.position_created"} <= actions


def test_existing_login_or_none_and_own_counter_name(admin, make_user):
    owner = make_user(Roles.SELLER, username="bakery")
    r = admin.post(
        URL,
        {"name": "Bakery", "login": "existing", "user": owner.pk, "counter_name": "Oven"},
        format="json",
    )
    assert r.status_code == 201, r.json()
    assert Seller.objects.get(name="Bakery").user == owner
    assert ServicePosition.objects.get(seller__name="Bakery").name == "Oven"
    r = admin.post(URL, {"name": "Kiosk", "login": "none"}, format="json")
    assert r.status_code == 201 and Seller.objects.get(name="Kiosk").user is None
    assert RFIDDevice.objects.count() == 0  # no reader asked for


def test_every_problem_at_its_field_and_nothing_saved(admin, make_user):
    other = Seller.objects.create(name="Peak Cafe")
    make_user(Roles.SELLER, username="taken")
    busy = till("Reader1", seller=other)
    r = admin.post(
        URL,
        {"name": "peak cafe", "username": "taken", "password": "123", "till_reader": busy.pk},
        format="json",
    )
    assert r.status_code == 400
    details = r.json()["error"]["details"]
    assert {"name", "username", "till_reader"} <= set(details), details
    assert Seller.objects.count() == 1 and not User.objects.filter(username="peakcafe").exists()
    assert "already assigned to Peak Cafe" in details["till_reader"][0]


def test_new_login_needs_username_and_password(admin):
    r = admin.post(URL, {"name": "Shop"}, format="json")
    details = r.json()["error"]["details"]
    assert {"username", "password"} <= set(details)
    assert not Seller.objects.exists()


def test_weak_password_is_refused(admin):
    r = admin.post(URL, {"name": "Shop", "username": "shop", "password": "123"}, format="json")
    assert "password" in str(r.json()["error"]["details"])
    assert not User.objects.filter(username="shop").exists()


def test_existing_login_must_be_a_seller(admin, make_user):
    developer = make_user(Roles.DEVELOPER, username="dev")
    r = admin.post(URL, {"name": "Shop", "login": "existing", "user": developer.pk}, format="json")
    assert r.status_code == 400 and "user" in r.json()["error"]["details"]


@pytest.mark.parametrize("role", [Roles.BOSS, Roles.MANAGER, Roles.FINANCE_MANAGER])
def test_only_who_may_create_sellers(auth_client, make_user, role):
    client = auth_client(make_user(role))
    r = client.post(URL, {"name": "Shop", "login": "none"}, format="json")
    assert r.status_code == 403 and not Seller.objects.exists()
