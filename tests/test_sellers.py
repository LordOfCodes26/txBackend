import pytest

from apps.accounts.rbac import Roles
from apps.audit.models import AuditLog
from apps.goods.models import Good
from apps.sellers.models import Seller, SellerStatus, ServicePosition

pytestmark = pytest.mark.django_db

SELLERS = "/api/v1/sellers/"
POSITIONS = "/api/v1/service-positions/"


@pytest.fixture
def seller_manager(auth_client, make_user):
    return auth_client(make_user(Roles.ADMIN))


@pytest.fixture
def seller_user(make_user):
    user = make_user(Roles.SELLER, email="shop@example.com")
    Seller.objects.create(name="Cafe", user=user)
    return user


def test_create_and_update_seller_is_audited(seller_manager, make_user):
    user = make_user(Roles.SELLER)
    response = seller_manager.post(SELLERS, {"name": "Cafe", "user": user.pk})
    assert response.status_code == 201, response.json()
    seller_id = response.json()["id"]

    response = seller_manager.patch(f"{SELLERS}{seller_id}/", {"status": "SUSPENDED"})
    assert response.json()["status"] == "SUSPENDED"
    actions = list(AuditLog.objects.order_by("id").values_list("action", flat=True))
    assert actions == ["seller.created", "seller.updated"]


def test_seller_name_and_user_are_unique(seller_manager, seller_user):
    response = seller_manager.post(SELLERS, {"name": "CAFE"})
    assert "name" in response.json()["error"]["details"]
    response = seller_manager.post(SELLERS, {"name": "Other", "user": seller_user.pk})
    assert "user" in response.json()["error"]["details"]


def test_sellers_cannot_be_deleted(seller_manager, seller_user):
    seller = Seller.objects.get()
    assert seller_manager.delete(f"{SELLERS}{seller.pk}/").status_code in (403, 405)


@pytest.mark.parametrize(
    ("role", "view", "create"),
    [
        (Roles.ADMIN, 200, 201),
        (Roles.MANAGER, 200, 403),
        (Roles.FINANCE_MANAGER, 403, 403),
        (Roles.DEVELOPER, 403, 403),
        (Roles.SELLER, 403, 403),
    ],
)
def test_seller_access_by_role(auth_client, make_user, role, view, create):
    client = auth_client(make_user(role))
    assert client.get(SELLERS).status_code == view
    assert client.post(SELLERS, {"name": f"S-{role}"}).status_code == create


def test_seller_me(auth_client, seller_user, make_user):
    assert auth_client(seller_user).get(f"{SELLERS}me/").json()["name"] == "Cafe"
    response = auth_client(make_user()).get(f"{SELLERS}me/")
    assert response.json()["error"]["code"] == "SELLER_PROFILE_NOT_FOUND"


# --- Service positions --------------------------------------------------------


def test_seller_manages_own_positions(auth_client, seller_user):
    client = auth_client(seller_user)
    response = client.post(POSITIONS, {"name": "Counter 1", "location": "Lobby"})
    assert response.status_code == 201, response.json()
    assert response.json()["seller"] == Seller.objects.get().pk

    other = Seller.objects.create(name="Other")
    theirs = ServicePosition.objects.create(seller=other, name="Theirs")
    assert [p["name"] for p in client.get(POSITIONS).json()["results"]] == ["Counter 1"]
    assert client.get(f"{POSITIONS}{theirs.pk}/").status_code == 404
    assert client.patch(f"{POSITIONS}{theirs.pk}/", {"name": "x"}).status_code == 404

    response = client.post(POSITIONS, {"name": "Sneaky", "seller": other.pk})
    assert response.status_code == 400


def test_suspended_seller_loses_catalog_access(auth_client, seller_user):
    Seller.objects.update(status=SellerStatus.SUSPENDED)
    assert auth_client(seller_user).get(POSITIONS).status_code == 403


def test_manager_must_choose_seller(seller_manager):
    response = seller_manager.post(POSITIONS, {"name": "Counter"})
    assert "seller" in response.json()["error"]["details"]


def test_position_name_unique_per_seller(seller_manager):
    a, b = Seller.objects.create(name="A"), Seller.objects.create(name="B")
    seller_manager.post(POSITIONS, {"name": "Counter", "seller": a.pk})
    assert seller_manager.post(POSITIONS, {"name": "counter", "seller": a.pk}).status_code == 400
    assert seller_manager.post(POSITIONS, {"name": "Counter", "seller": b.pk}).status_code == 201


def test_position_cannot_move_seller(seller_manager):
    a, b = Seller.objects.create(name="A"), Seller.objects.create(name="B")
    position = ServicePosition.objects.create(seller=a, name="P")
    assert seller_manager.patch(f"{POSITIONS}{position.pk}/", {"seller": b.pk}).status_code == 400


def test_position_with_goods_cannot_be_deleted(seller_manager):
    position = ServicePosition.objects.create(seller=Seller.objects.create(name="A"), name="P")
    good = Good.objects.create(service_position=position, name="Tea", price="1.00")
    response = seller_manager.delete(f"{POSITIONS}{position.pk}/")
    assert response.json()["error"]["code"] == "POSITION_HAS_GOODS"

    good.soft_delete()
    assert seller_manager.delete(f"{POSITIONS}{position.pk}/").status_code == 204
    assert ServicePosition.all_objects.get().deleted_at is not None


# --- Store logins: users with the SELLER role ------------------------------------------


def test_only_seller_role_users_can_be_linked(seller_manager, make_user):
    plain = make_user()
    response = seller_manager.post(SELLERS, {"name": "Shop", "user": plain.pk})
    assert response.json()["error"]["details"] == {
        "user": ["This user doesn't have the SELLER role."]
    }
    seller_login = make_user(Roles.SELLER, email="shop@x.com")
    response = seller_manager.post(SELLERS, {"name": "Shop", "user": seller_login.pk})
    assert (response.status_code, response.json()["user_username"]) == (201, "shop")

    position = ServicePosition.objects.create(seller_id=response.json()["id"], name="Till")
    r = seller_manager.patch(
        f"/api/v1/service-positions/{position.pk}/", {"manager": plain.pk}, format="json"
    )
    assert "manager" in r.json()["error"]["details"]


def test_store_access_needs_the_role_and_the_link(auth_client, make_user):
    from apps.accounts.models import UserRole
    from apps.goods.models import Good

    owner = make_user(Roles.SELLER)
    shop = Seller.objects.create(name="Shop", user=owner)
    other = Seller.objects.create(name="Other")
    for seller, name in [(shop, "Mine"), (other, "Theirs")]:
        position = ServicePosition.objects.create(seller=seller, name="Till")
        Good.objects.create(service_position=position, name=name, price="1.00")

    goods = auth_client(owner).get("/api/v1/goods/").json()["results"]
    assert [g["name"] for g in goods] == ["Mine"]

    UserRole.objects.filter(user=owner).delete()  # role taken away: no store access
    fresh = type(owner).objects.get(pk=owner.pk)  # as on the next request
    assert auth_client(fresh).get("/api/v1/goods/").status_code == 403


def test_positions_of_active_sellers_only(auth_client, make_user):
    from apps.sellers.models import Seller, ServicePosition

    open_shop = Seller.objects.create(name="Open")
    closed_shop = Seller.objects.create(name="Closed", status="CLOSED")
    ServicePosition.objects.create(seller=open_shop, name="Open counter")
    ServicePosition.objects.create(seller=open_shop, name="Old counter", is_active=False)
    ServicePosition.objects.create(seller=closed_shop, name="Closed counter")
    client = auth_client(make_user(Roles.ADMIN))
    r = client.get("/api/v1/service-positions/?is_active=true&seller_status=ACTIVE")
    assert [p["name"] for p in r.json()["results"]] == ["Open counter"]
