"""BUILDING_OWNER: like BOSS (read-only + statistics), limited to their own buildings,
including the stores there."""

from decimal import Decimal

import pytest
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Role, UserRole
from apps.accounts.rbac import VIEW, Roles
from apps.developers.models import Developer
from apps.finance import services as finance
from apps.finance.models import TransactionKind
from apps.goods.models import Good
from apps.purchases.models import Purchase, PurchaseKind
from apps.rfid.models import Building
from apps.sellers.models import Seller, ServicePosition

pytestmark = pytest.mark.django_db

STATS = "/api/v1/stats/"


@pytest.fixture
def site(make_user):
    b1 = Building.objects.create(code="B1", name="Building 1")
    b2 = Building.objects.create(code="B2", name="Building 2")
    owner = make_user(Roles.BUILDING_OWNER, email="owner1@x.com")
    b1.owners.add(owner)

    ada = Developer.objects.create(employee_number="E1", full_name="Ada", building=b1)
    bob = Developer.objects.create(employee_number="E2", full_name="Bob", building=b2)
    for dev, amount, key in [(ada, "100.00", "own-0001"), (bob, "300.00", "own-0002")]:
        finance.deposit(actor=None, developer=dev, amount=amount, idempotency_key=key)

    cafe = Seller.objects.create(name="Cafe")  # positions in both buildings
    shop = Seller.objects.create(name="Shop")  # only in Building 1
    cafe1 = ServicePosition.objects.create(seller=cafe, name="Cafe B1", building=b1)
    cafe2 = ServicePosition.objects.create(seller=cafe, name="Cafe B2", building=b2)
    shop1 = ServicePosition.objects.create(seller=shop, name="Shop B1", building=b1)
    for position in (cafe1, cafe2, shop1):
        Good.objects.create(service_position=position, name=f"Tea {position.name}", price="1.00")

    account = finance.open_account(ada)
    for position, total in [(cafe1, "10.00"), (cafe2, "40.00"), (shop1, "5.00")]:
        with transaction.atomic():
            txn = finance.post_transaction(
                account=account,
                kind=TransactionKind.PURCHASE,
                amount=-Decimal(total),
                actor=None,
                description="t",
                reference="t",
            )
            Purchase.objects.create(
                seller=position.seller,
                service_position=position,
                kind=PurchaseKind.SALE,
                status="CONFIRMED",
                developer=ada,
                total=Decimal(total),
                account_transaction=txn,
                confirmed_at=timezone.now(),
            )

    class S:
        pass

    s = S()
    s.__dict__.update(locals())
    return s


@pytest.fixture
def client(auth_client, site):
    return auth_client(site.owner)


def rows(response):
    body = response.json()
    return body["results"] if isinstance(body, dict) and "results" in body else body


def test_role_is_boss_minus_company_wide_lists():
    perms = set(
        Role.objects.get(code=Roles.BUILDING_OWNER).permissions.values_list("codename", flat=True)
    )
    assert perms == (VIEW - {"user.view", "role.view", "audit.view"}) | {"excel.export"}


def test_people_and_money_of_own_building(client, site):
    assert [d["full_name"] for d in rows(client.get("/api/v1/developers/"))] == ["Ada"]
    accounts = rows(client.get("/api/v1/finance/accounts/"))
    assert [a["developer"]["full_name"] for a in accounts] == ["Ada"]
    txns = rows(client.get("/api/v1/finance/transactions/"))
    assert txns and {t["account"] for t in txns} == {site.account.pk}


def test_stores_in_own_building(client, site):
    assert sorted(s["name"] for s in rows(client.get("/api/v1/sellers/"))) == ["Cafe", "Shop"]
    positions = sorted(p["name"] for p in rows(client.get("/api/v1/service-positions/")))
    assert positions == ["Cafe B1", "Shop B1"]
    goods = sorted(g["name"] for g in rows(client.get("/api/v1/goods/")))
    assert goods == ["Tea Cafe B1", "Tea Shop B1"]
    sales = {p["service_position"] for p in rows(client.get("/api/v1/purchases/"))}
    assert sales == {site.cafe1.pk, site.shop1.pk}
    perf = client.get("/api/v1/purchases/performance/").json()
    assert sorted(r["service_position_name"] for r in perf) == ["Cafe B1", "Shop B1"]


def test_read_only_and_no_company_wide_lists(client, site):
    for path in ("/api/v1/users/", "/api/v1/roles/", "/api/v1/audit-logs/"):
        assert client.get(path).status_code == 403, path
    assert (
        client.post("/api/v1/developers/", {"employee_number": "E9", "full_name": "X"}).status_code
        == 403
    )
    assert client.patch(f"/api/v1/sellers/{site.shop.pk}/", {"name": "X"}).status_code == 403
    r = client.post(
        "/api/v1/finance/deposits/",
        {"developer": site.ada.pk, "amount": "1.00"},
        HTTP_IDEMPOTENCY_KEY="own-deposit-01",
    )
    assert r.status_code == 403


def test_statistics_of_own_buildings(client, site):
    body = client.get(STATS).json()
    assert body["buildings"] == [{"id": site.b1.pk, "code": "B1", "name": "Building 1"}]
    people = body["people"]
    assert people["developers"]["total"] == 1
    assert [b["name"] for b in people["inside_now"]["buildings"]] == ["Building 1"]
    money = body["money"]
    assert money["deposits"] == {"total": "100.00", "count": 1}
    assert money["spending"] == {"total": "55.00", "count": 3}  # Ada's purchases anywhere
    assert money["developer_accounts"]["total_balance"] == "45.00"
    assert money["store_sales"] == {
        "sales_total": "15.00",  # Cafe B1 + Shop B1, not Cafe B2
        "sales_count": 2,
        "bookings_total": "0.00",
        "bookings_count": 0,
    }
    assert "sellers" not in money  # no seller balances any more


def test_boss_sees_the_whole_company(auth_client, make_user, site):
    body = auth_client(make_user(Roles.BOSS)).get(STATS).json()
    assert body["buildings"] is None
    assert body["money"]["store_sales"]["sales_total"] == "55.00"
    assert body["money"]["deposits"]["total"] == "400.00"


def test_owner_with_another_unrestricted_role_sees_everything(auth_client, site):
    UserRole.objects.create(user=site.owner, role=Role.objects.get(code=Roles.BOSS))
    body = auth_client(site.owner).get(STATS).json()
    assert body["buildings"] is None


def test_owner_without_a_building_sees_nothing(auth_client, make_user, site):
    client = auth_client(make_user(Roles.BUILDING_OWNER))
    assert rows(client.get("/api/v1/developers/")) == []
    body = client.get(STATS).json()
    assert body["buildings"] == []
    assert body["money"]["deposits"]["total"] == "0.00"


def test_admin_assigns_owners_but_building_managers_cannot(auth_client, admin, make_user, site):
    other = make_user(Roles.BUILDING_OWNER)
    r = auth_client(admin).patch(
        f"/api/v1/rfid/buildings/{site.b2.pk}/", {"owners": [other.pk]}, format="json"
    )
    assert r.json()["owners"] == [other.pk]
    manager = make_user(Roles.BUILDING_MANAGER)
    site.b2.managers.add(manager)
    r = auth_client(manager).patch(
        f"/api/v1/rfid/buildings/{site.b2.pk}/", {"owners": []}, format="json"
    )
    assert "owners" in r.json()["error"]["details"]


def test_owners_need_the_building_owner_role(auth_client, admin, make_user, site):
    plain = make_user(email="plain@x.com")
    r = auth_client(admin).patch(
        f"/api/v1/rfid/buildings/{site.b2.pk}/", {"owners": [plain.pk]}, format="json"
    )
    assert r.json()["error"]["details"] == {
        "owners": ["These users don't have the BUILDING_OWNER role: plain"]
    }
    r = auth_client(admin).patch(
        f"/api/v1/rfid/buildings/{site.b2.pk}/", {"managers": [site.owner.pk]}, format="json"
    )
    assert "managers" in r.json()["error"]["details"]
