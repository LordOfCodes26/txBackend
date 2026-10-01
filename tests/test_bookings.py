import threading
import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.accounts.rbac import Roles
from apps.audit.models import AuditLog
from apps.bookings import services
from apps.bookings.exceptions import SlotUnavailable
from apps.bookings.models import Booking
from apps.developers.models import Developer
from apps.finance import services as finance
from apps.finance.models import DeveloperAccount
from apps.goods.models import Good, GoodKind, RentalSettings
from apps.purchases.models import Purchase
from apps.seller_finance.services import seller_ledger_mismatches
from apps.sellers.models import Seller, ServicePosition

GOODS = "/api/v1/goods/"
RENTALS = "/api/v1/rentals/"
BOOKINGS = "/api/v1/bookings/"
PIN = "4826"


def key():
    return str(uuid.uuid4())


def tomorrow_at(hour, minute=0):
    day = timezone.localdate() + timedelta(days=1)
    return timezone.make_aware(datetime.combine(day, time(hour, minute)))


@pytest.fixture
def world(db, make_user):
    seller_user = make_user(Roles.SELLER, email="sports@x.com")
    seller = Seller.objects.create(name="Sports Center", user=seller_user)
    position = ServicePosition.objects.create(seller=seller, name="Outdoor", location="Garden")
    playground = Good.objects.create(
        service_position=position,
        name="Playground",
        price="20.00",
        kind=GoodKind.RENTAL,
        track_stock=False,
    )
    RentalSettings.objects.create(
        good=playground,
        slot_minutes=60,
        opening_time=time(8),
        closing_time=time(20),
        max_slots_per_booking=3,
        max_days_ahead=14,
    )
    dev_user = make_user(Roles.DEVELOPER, email="ada@x.com")
    developer = Developer.objects.create(employee_number="E1", full_name="Ada", user=dev_user)
    account = finance.open_account(developer)
    finance.deposit(actor=None, developer=developer, amount="100.00", idempotency_key="seed-0001")
    finance.set_pin(actor=None, account=account, pin=PIN, current_pin=None)

    class W:
        pass

    w = W()
    w.__dict__.update(locals())
    return w


@pytest.fixture
def dev_client(auth_client, world):
    return auth_client(world.dev_user)


def book(client, world, start, slots=1, pin=PIN, idem=None, good=None):
    return client.post(
        BOOKINGS,
        {
            "good": (good or world.playground).pk,
            "start": start.isoformat(),
            "slots": slots,
            "pin": pin,
        },
        HTTP_IDEMPOTENCY_KEY=idem or key(),
    )


def balance(world):
    return DeveloperAccount.objects.get(pk=world.account.pk).balance


# --- Creating rental goods ---------------------------------------------------------------


@pytest.mark.django_db
def test_seller_creates_rental_with_settings(auth_client, world):
    client = auth_client(world.seller_user)
    response = client.post(
        GOODS,
        {
            "service_position": world.position.pk,
            "name": "Pool",
            "price": "15.00",
            "kind": "RENTAL",
            "rental": {
                "slot_minutes": 30,
                "opening_time": "09:00",
                "closing_time": "18:00",
                "weekdays": [5, 6, 5],
            },
        },
        format="json",
    )
    assert response.status_code == 201, response.json()
    body = response.json()
    assert (body["kind"], body["track_stock"], body["quantity"]) == ("RENTAL", False, 0)
    assert body["rental"]["weekdays"] == [5, 6]
    assert body["rental"]["max_slots_per_booking"] == 4

    # Editing rules is audited with the rest of the good.
    client.patch(f"{GOODS}{body['id']}/", {"rental": {"closing_time": "19:00"}}, format="json")
    log = AuditLog.objects.filter(action="good.updated").latest("id")
    assert log.new_values["rental"]["closing_time"] == "19:00:00"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"kind": "RENTAL"}, "rental"),
        (
            {
                "kind": "RENTAL",
                "track_stock": True,
                "rental": {"slot_minutes": 60, "opening_time": "08:00", "closing_time": "20:00"},
            },
            "track_stock",
        ),
        (
            {
                "kind": "RENTAL",
                "rental": {"slot_minutes": 60, "opening_time": "20:00", "closing_time": "08:00"},
            },
            "rental",
        ),
        ({"kind": "SERVICE", "initial_quantity": 5}, "initial_quantity"),
        (
            {
                "kind": "PRODUCT",
                "rental": {"slot_minutes": 60, "opening_time": "08:00", "closing_time": "20:00"},
            },
            "rental",
        ),
    ],
)
def test_kind_validation(auth_client, world, payload, field):
    client = auth_client(world.seller_user)
    body = {"service_position": world.position.pk, "name": "X", "price": "1.00"} | payload
    response = client.post(GOODS, body, format="json")
    assert response.status_code == 400
    assert field in response.json()["error"]["details"]


@pytest.mark.django_db
def test_kind_cannot_change(auth_client, world):
    client = auth_client(world.seller_user)
    response = client.patch(f"{GOODS}{world.playground.pk}/", {"kind": "PRODUCT"})
    assert "kind" in response.json()["error"]["details"]


@pytest.mark.django_db
def test_database_forbids_stock_on_non_products(world):
    with pytest.raises(IntegrityError), transaction.atomic():
        Good.objects.filter(pk=world.playground.pk).update(track_stock=True)


@pytest.mark.django_db
def test_rentals_cannot_be_sold_at_the_till(auth_client, world):
    client = auth_client(world.seller_user)
    pid = client.post("/api/v1/purchases/", {"service_position": world.position.pk}).json()["id"]
    response = client.post(f"/api/v1/purchases/{pid}/items/", {"good": world.playground.pk})
    assert response.json()["error"]["code"] == "GOOD_NOT_AVAILABLE"


# --- Browsing and availability ----------------------------------------------------------


@pytest.mark.django_db
def test_developer_browses_rentals_and_availability(dev_client, world):
    rentals = dev_client.get(RENTALS).json()["results"]
    assert [r["name"] for r in rentals] == ["Playground"]
    assert rentals[0]["rental"]["slot_minutes"] == 60

    day = tomorrow_at(0).date()
    slots = dev_client.get(f"{RENTALS}{world.playground.pk}/availability/?date={day}").json()
    assert len(slots) == 12  # 08:00-20:00 in 1h slots
    assert all(s["available"] for s in slots)

    book(dev_client, world, tomorrow_at(10), slots=2)
    slots = dev_client.get(f"{RENTALS}{world.playground.pk}/availability/?date={day}").json()
    taken = [
        timezone.localtime(datetime.fromisoformat(s["start"])).hour
        for s in slots
        if not s["available"]
    ]
    assert taken == [10, 11]


@pytest.mark.django_db
def test_inactive_rental_is_hidden(dev_client, world):
    Good.objects.filter(pk=world.playground.pk).update(is_active=False)
    assert dev_client.get(RENTALS).json()["count"] == 0
    assert (
        book(dev_client, world, tomorrow_at(10)).json()["error"]["code"] == "RENTAL_NOT_AVAILABLE"
    )


@pytest.mark.django_db
def test_closed_weekday_has_no_slots(dev_client, world):
    day = tomorrow_at(0).date()
    RentalSettings.objects.filter(good=world.playground).update(
        weekdays=[d for d in range(7) if d != day.weekday()]
    )
    assert dev_client.get(f"{RENTALS}{world.playground.pk}/availability/?date={day}").json() == []
    assert book(dev_client, world, tomorrow_at(10)).json()["error"]["code"] == "INVALID_SLOT"


# --- Booking --------------------------------------------------------------------------


@pytest.mark.django_db
def test_booking_charges_developer_and_credits_seller(dev_client, world):
    response = book(dev_client, world, tomorrow_at(14), slots=2)
    assert response.status_code == 201, response.json()
    body = response.json()
    assert (body["total"], body["balance_after"], body["slots"]) == ("40.00", "60.00", 2)
    assert timezone.localtime(datetime.fromisoformat(body["end"])).hour == 16

    assert balance(world) == Decimal("60.00")
    purchase = Purchase.objects.get(pk=body["purchase"])
    assert (purchase.status, purchase.total, purchase.developer_id) == (
        "CONFIRMED",
        Decimal("40.00"),
        world.developer.pk,
    )
    assert world.seller.account.balance == Decimal("40.00")
    assert AuditLog.objects.filter(action="booking.created").exists()
    assert finance.ledger_mismatches() == [] and seller_ledger_mismatches() == []


@pytest.mark.django_db
def test_overlapping_booking_is_rejected(dev_client, world):
    assert book(dev_client, world, tomorrow_at(10), slots=2).status_code == 201  # 10-12
    r = book(dev_client, world, tomorrow_at(11))
    assert (r.status_code, r.json()["error"]["code"]) == (409, "SLOT_UNAVAILABLE")
    assert book(dev_client, world, tomorrow_at(12)).status_code == 201  # back-to-back is fine
    assert balance(world) == Decimal("40.00")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "start_fn",
    [
        lambda: tomorrow_at(10, 30),  # off the slot grid
        lambda: tomorrow_at(7),  # before opening
        lambda: tomorrow_at(19, 0),  # 2 slots would end at 21:00
        lambda: timezone.now() - timedelta(hours=1),  # in the past
        lambda: tomorrow_at(10) + timedelta(days=20),  # beyond max_days_ahead
    ],
)
def test_invalid_slots(dev_client, world, start_fn):
    response = book(dev_client, world, start_fn(), slots=2)
    assert response.json()["error"]["code"] == "INVALID_SLOT", response.json()
    assert balance(world) == Decimal("100.00")


@pytest.mark.django_db
def test_too_many_slots(dev_client, world):
    r = book(dev_client, world, tomorrow_at(9), slots=4)
    assert r.json()["error"]["details"] == {"max_slots_per_booking": 3}


@pytest.mark.django_db
def test_booking_needs_pin_and_balance(dev_client, world):
    assert (
        book(dev_client, world, tomorrow_at(9), pin="0000").json()["error"]["code"] == "INVALID_PIN"
    )
    Good.objects.filter(pk=world.playground.pk).update(price="60.00")
    r = book(dev_client, world, tomorrow_at(9), slots=2)
    assert r.json()["error"]["code"] == "INSUFFICIENT_BALANCE"
    assert not Booking.objects.exists() and not Purchase.objects.exists()


@pytest.mark.django_db
def test_booking_retry_is_idempotent(dev_client, world):
    idem = key()
    first = book(dev_client, world, tomorrow_at(9), idem=idem)
    again = book(dev_client, world, tomorrow_at(9), idem=idem)
    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json()["id"] == again.json()["id"]
    assert balance(world) == Decimal("80.00")
    other = book(dev_client, world, tomorrow_at(15), idem=idem)
    assert other.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.django_db
def test_database_rejects_overlap_directly(world, dev_client):
    book(dev_client, world, tomorrow_at(10))
    existing = Booking.objects.get()
    with pytest.raises(IntegrityError), transaction.atomic():
        Booking.objects.create(
            good=world.playground,
            developer=world.developer,
            purchase=existing.purchase,
            start=tomorrow_at(10, 30),
            end=tomorrow_at(11, 30),
            slots=1,
        )


# --- Visibility ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_who_sees_bookings(auth_client, make_user, world, dev_client):
    book(dev_client, world, tomorrow_at(9))
    mine = dev_client.get(f"{BOOKINGS}me/").json()["results"]
    assert [b["good_name"] for b in mine] == ["Playground"]
    assert dev_client.get(BOOKINGS).status_code == 403

    seller_view = auth_client(world.seller_user).get(BOOKINGS).json()["results"]
    assert [b["developer"]["full_name"] for b in seller_view] == ["Ada"]

    other = make_user(Roles.SELLER)
    Seller.objects.create(name="Other", user=other)
    assert auth_client(other).get(BOOKINGS).json()["count"] == 0
    assert auth_client(make_user(Roles.SELLER_MANAGER)).get(BOOKINGS).json()["count"] == 1


# --- Concurrency -------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_two_developers_race_for_the_same_slot(world, make_user):
    dev2 = Developer.objects.create(employee_number="E2", full_name="Bob")
    acc2 = finance.open_account(dev2)
    finance.deposit(actor=None, developer=dev2, amount="100.00", idempotency_key="seed-0002")
    finance.set_pin(actor=None, account=acc2, pin="5082", current_pin=None)
    start = tomorrow_at(10)

    barrier = threading.Barrier(2)
    outcomes = []

    def attempt(developer, pin):
        try:
            barrier.wait()
            outcomes.append(
                services.book(
                    actor=None,
                    developer=developer,
                    good=world.playground,
                    start=start,
                    slots=1,
                    pin=pin,
                    idempotency_key=key(),
                )
            )
        except SlotUnavailable as exc:
            outcomes.append(exc)
        finally:
            connection.close()

    threads = [
        threading.Thread(target=attempt, args=a) for a in ((world.developer, PIN), (dev2, "5082"))
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(isinstance(o, tuple) for o in outcomes) == 1, outcomes
    assert Booking.objects.count() == 1
    total = (
        DeveloperAccount.objects.get(pk=world.account.pk).balance
        + DeveloperAccount.objects.get(pk=acc2.pk).balance
    )
    assert total == Decimal("180.00")  # exactly one 20.00 charge
    assert finance.ledger_mismatches() == [] and seller_ledger_mismatches() == []
