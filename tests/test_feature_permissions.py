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
def test_checker_sees_indoor_bookings_with_three_states(auth_client, make_user):
    from datetime import time, timedelta

    from django.utils import timezone

    from apps.bookings.models import Booking
    from apps.goods.models import Good, GoodKind, RentalSettings
    from apps.sellers.models import Seller, ServicePosition
    from tests.test_bookings import tomorrow_at  # noqa: F401

    position = ServicePosition.objects.create(
        seller=Seller.objects.create(name="Play"), name="Desk"
    )
    courts = {}
    for name, place in (("Hall", "INDOOR"), ("Field", "OUTDOOR")):
        good = Good.objects.create(
            service_position=position,
            name=name,
            price="10.00",
            kind=GoodKind.RENTAL,
            track_stock=False,
        )
        RentalSettings.objects.create(
            good=good, slot_minutes=60, opening_time=time(8), closing_time=time(20), place=place
        )
        courts[name] = good
    ada = Developer.objects.create(employee_number="E1", full_name="Ada")
    start = timezone.now().replace(minute=0, second=0, microsecond=0) + timedelta(days=1)
    rows = [
        ("Hall", 0, "RESERVED", None),
        ("Hall", 2, "PAID", None),
        ("Hall", 4, "PAID", timezone.now()),
        ("Field", 6, "PAID", None),
    ]
    for court, hour, status, checked in rows:
        Booking.objects.create(
            good=courts[court],
            developer=ada,
            start=start + timedelta(hours=hour),
            end=start + timedelta(hours=hour + 1),
            slots=1,
            status=status,
            checked_in_at=checked,
        )
    checker = auth_client(make_user(Roles.PLAYGROUND_CHECKER))
    listed = checker.get("/api/v1/bookings/").json()["results"]
    assert sorted(r["good_name"] for r in listed) == ["Hall", "Hall", "Hall"]
    states = {
        state: checker.get("/api/v1/bookings/", {"state": state}).json()["count"]
        for state in ("NOT_PAID", "PAID", "CHECKED_IN")
    }
    assert states == {"NOT_PAID": 1, "PAID": 1, "CHECKED_IN": 1}
    admin = auth_client(make_user(Roles.ADMIN))
    assert admin.get("/api/v1/bookings/").json()["count"] == 4


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


@pytest.mark.django_db
def test_checker_checks_in_a_paid_indoor_booking_with_a_card_tap(auth_client, make_user):
    from datetime import time, timedelta

    from django.utils import timezone

    from apps.bookings.models import Booking, PlaygroundCheck
    from apps.goods.models import Good, GoodKind, RentalSettings
    from apps.rfid import services as rfid
    from apps.rfid.models import RFIDCard, RFIDCardAssignment
    from apps.sellers.models import Seller, ServicePosition

    position = ServicePosition.objects.create(
        seller=Seller.objects.create(name="Play"), name="Desk"
    )
    hall = Good.objects.create(
        service_position=position,
        name="Hall",
        price="10.00",
        kind=GoodKind.RENTAL,
        track_stock=False,
    )
    RentalSettings.objects.create(
        good=hall, slot_minutes=60, opening_time=time(0), closing_time=time(23), place="INDOOR"
    )
    ada = Developer.objects.create(employee_number="E1", full_name="Ada")
    RFIDCardAssignment.objects.create(card=RFIDCard.objects.create(uid="04AA"), developer=ada)
    bob = Developer.objects.create(employee_number="E2", full_name="Bob")
    RFIDCardAssignment.objects.create(card=RFIDCard.objects.create(uid="04BB"), developer=bob)
    # Two hours ahead: the reader's own check (20 minutes early at most) finds nothing.
    start = timezone.now() + timedelta(hours=2)
    paid = Booking.objects.create(
        good=hall,
        developer=ada,
        start=start,
        end=start + timedelta(hours=1),
        slots=1,
        status="PAID",
    )
    user = make_user(Roles.PLAYGROUND_CHECKER)
    gym = Seller.objects.create(name="Gym", user=user, checks_bookings=True)
    ServicePosition.objects.create(seller=gym, name="Gym counter")
    reader, _key = rfid.register_device(actor=None, code="Gate9", purpose="TILL", seller=gym)
    checker = auth_client(user)
    url = f"/api/v1/bookings/{paid.pk}/indoor-check-in/"

    assert checker.post(url, {}).json()["error"]["code"] == "VALIDATION_ERROR"  # no tap
    rfid.record_scan(device=reader, uid="04BB")  # Bob's card
    bobs = PlaygroundCheck.objects.latest("pk")
    assert checker.post(url, {"check": bobs.pk}).json()["error"]["code"] == "CARD_NOT_THE_BOOKERS"
    rfid.record_scan(device=reader, uid="04AA")  # Ada's card
    adas = PlaygroundCheck.objects.latest("pk")
    r = checker.post(url, {"check": adas.pk})
    assert r.status_code == 200, r.json()
    assert r.json()["checked_in_at"] is not None
    assert auth_client(make_user(Roles.MANAGER)).post(url, {"check": adas.pk}).status_code == 403


@pytest.mark.django_db
def test_checker_sees_indoor_courts_only(auth_client, make_user):
    from datetime import time

    from apps.goods.models import Good, GoodKind, RentalSettings
    from apps.sellers.models import Seller, ServicePosition

    position = ServicePosition.objects.create(
        seller=Seller.objects.create(name="Play"), name="Desk"
    )
    for name, place in (("Hall", "INDOOR"), ("Field", "OUTDOOR")):
        good = Good.objects.create(
            service_position=position,
            name=name,
            price="10.00",
            kind=GoodKind.RENTAL,
            track_stock=False,
        )
        RentalSettings.objects.create(
            good=good, slot_minutes=60, opening_time=time(0), closing_time=time(23), place=place
        )
    checker = auth_client(make_user(Roles.PLAYGROUND_CHECKER))
    assert [r["name"] for r in checker.get("/api/v1/rentals/").json()["results"]] == ["Hall"]
    admin = auth_client(make_user(Roles.ADMIN))
    assert {r["name"] for r in admin.get("/api/v1/rentals/").json()["results"]} == {"Hall", "Field"}
