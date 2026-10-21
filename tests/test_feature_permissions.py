"""Own permissions for newer features: goods categories, the deposit list, the sales
report and taking booking payments. A role can get one without the rest."""

import importlib
import uuid

import pytest

from apps.accounts.models import Permission, Role, UserRole
from apps.accounts.rbac import ROLES, Roles
from apps.developers.models import Developer
from apps.finance import services as finance


@pytest.fixture
def role_with(make_user, auth_client):
    def _make(*codes):
        role = Role.objects.create(code=f"R{uuid.uuid4().hex[:6]}", name="Custom")
        role.permissions.add(*Permission.objects.filter(codename__in=codes))
        user = make_user()
        UserRole.objects.create(user=user, role=role)
        return auth_client(user)

    return _make


@pytest.mark.django_db
def test_deposit_list_without_seeing_every_transaction(role_with):
    ada = Developer.objects.create(employee_number="E1", full_name="Ada")
    finance.deposit(actor=None, developer=ada, amount="5.00", idempotency_key="d-1", method="CASH")
    finance.adjust(actor=None, developer=ada, amount="-1.00", reason="Fix", idempotency_key="a-1")
    client = role_with("finance.deposits.view")
    assert client.get("/api/v1/finance/transactions/").status_code == 403
    rows = client.get("/api/v1/finance/transactions/deposits/").json()["results"]
    assert [(r["kind"], r["amount"], r["method"]) for r in rows] == [("DEPOSIT", "5.00", "CASH")]
    assert (
        role_with("finance.view").get("/api/v1/finance/transactions/deposits/").status_code == 403
    )


@pytest.mark.django_db
def test_categories_and_sales_report_need_their_own_permission(role_with):
    goods_viewer = role_with("good.view", "good.update")
    assert goods_viewer.get("/api/v1/good-categories/").status_code == 200
    assert (
        goods_viewer.post("/api/v1/good-categories/", {"name": "X", "seller": 1}).status_code == 403
    )
    assert goods_viewer.get("/api/v1/goods/statistics/").status_code == 403
    assert role_with("purchase.report.view").get("/api/v1/goods/statistics/").status_code == 200


@pytest.mark.django_db
def test_booking_payments_need_booking_pay(role_with):
    assert role_with("booking.create").post("/api/v1/bookings/checkout/", {}).status_code == 403
    # booking.pay alone may start one (here refused for the missing fields, not the right).
    assert role_with("booking.pay").post("/api/v1/bookings/checkout/", {}).status_code == 400


def test_built_in_roles_keep_what_they_could_do():
    finance_manager = ROLES[Roles.FINANCE_MANAGER].permissions
    assert {"finance.deposits.view", "purchase.report.view"} <= finance_manager
    assert {"finance.deposits.view", "purchase.report.view"} <= ROLES[Roles.BOSS].permissions
    assert "good.category" not in ROLES[Roles.BOSS].permissions  # read-only


@pytest.mark.django_db
def test_the_migration_gives_custom_roles_the_new_permissions():
    """A role made by an admin keeps what it could do."""
    migration = importlib.import_module("apps.accounts.migrations.0011_feature_permissions")
    Permission.objects.filter(codename__in=migration.FROM.values()).delete()
    role = Role.objects.create(code="SHOP_HELP", name="Shop help")
    role.permissions.add(*Permission.objects.filter(codename__in=["good.update", "booking.create"]))

    from django.apps import apps

    migration.forward(apps, None)
    assert set(role.permissions.values_list("codename", flat=True)) == {
        "good.update",
        "good.category",
        "booking.create",
        "booking.pay",
    }


@pytest.mark.django_db
def test_store_and_counter_counts(auth_client, make_user):
    from apps.goods.models import Good
    from apps.rfid import services as rfid
    from apps.sellers.models import Seller, ServicePosition

    store = Seller.objects.create(name="Cafe")
    counter = ServicePosition.objects.create(seller=store, name="Bar")
    ServicePosition.objects.create(seller=store, name="Old", is_active=False)
    Good.objects.create(service_position=counter, name="Tea", price="1.00")
    Good.objects.create(service_position=counter, name="Gone", price="1.00").delete()
    rfid.register_device(actor=None, code="Till9", purpose="TILL", seller=store)
    admin = auth_client(make_user(Roles.ADMIN))
    row = next(r for r in admin.get("/api/v1/sellers/").json()["results"] if r["name"] == "Cafe")
    assert (
        row["counters_count"],
        row["goods_count"],
        row["courts_count"],
        row["till_readers_count"],
    ) == (1, 1, 0, 1)
    bar = admin.get(f"/api/v1/service-positions/{counter.pk}/").json()
    assert (bar["goods_count"], bar["courts_count"]) == (1, 0)
