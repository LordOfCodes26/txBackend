"""A court's price list: several prices (whole court, per person), like goods' prices. The
desk picks one when booking; the court's own price follows its first price."""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.bookings.models import Booking
from apps.goods.models import CourtPrice, Good
from apps.rfid import services as rfid
from tests.test_bookings import PIN, UID, balance, desk, tomorrow_at, world  # noqa: F401

GOODS = "/api/v1/goods/"
BOOKINGS = "/api/v1/bookings/"
CHECKOUT = "/api/v1/bookings/checkout/"


def prices(world):  # noqa: F811
    return list(CourtPrice.objects.filter(good=world.playground, is_active=True))


def add(desk, world, name, price, per="COURT"):  # noqa: F811
    return desk.post(
        f"{GOODS}{world.playground.pk}/prices/", {"name": name, "price": price, "per": per}
    )


def reserve(desk, world, start, slots=2, people=1, price=None):  # noqa: F811
    local = timezone.localtime(start)
    body = {
        "good": world.playground.pk,
        "developer": world.developer.pk,
        "date": local.date(),
        "start_time": f"{local:%H:%M}",
        "end_time": f"{local + timedelta(hours=slots):%H:%M}",
        "people": people,
    }
    if price is not None:
        body["price"] = price
    return desk.post(f"{BOOKINGS}reserve/", body)


@pytest.fixture
def two_prices(desk, world):  # noqa: F811
    whole = add(desk, world, "Whole court", "20.00")
    assert whole.status_code == 201, whole.json()
    person = add(desk, world, "Per person", "5.00", "PERSON")
    assert person.status_code == 201, person.json()
    return whole.json()["id"], person.json()["id"]


@pytest.mark.django_db
def test_a_new_court_starts_with_its_price(desk):  # noqa: F811
    r = desk.post(
        GOODS,
        {
            "name": "Pool",
            "kind": "RENTAL",
            "price": "12.00",
            "rental": {"slot_minutes": 60, "opening_time": "08:00", "closing_time": "20:00"},
        },
        format="json",
    )
    assert r.status_code == 201, r.json()
    assert [(p["name"], p["price"], p["per"]) for p in r.json()["court_prices"]] == [
        ("Whole court", "12.00", "COURT")
    ]


@pytest.mark.django_db
def test_book_with_a_per_person_price_and_pay_it(desk, world, two_prices):  # noqa: F811
    _whole, person = two_prices
    court = desk.get(f"/api/v1/rentals/{world.playground.pk}/").json()
    assert [p["name"] for p in court["prices"]] == ["Whole court", "Per person"]

    booking = reserve(desk, world, tomorrow_at(14), slots=2, people=3, price=person).json()
    assert (booking["price"], booking["price_name"], booking["people"]) == (
        "30.00",  # 5 x 2 slots x 3 people
        "Per person",
        3,
    )
    device, _key = rfid.register_device(actor=None, code="DESK1", purpose="ENROLL")
    event, _ = rfid.record_scan(device=device, uid=UID, client_event_id=str(uuid.uuid4()))
    pay = desk.post(f"{BOOKINGS}{booking['id']}/pay/", {"reader": "DESK1", "event": event.pk})
    assert pay.status_code == 201, pay.json()
    assert (pay.json()["total"], pay.json()["items"][0]["price_name"]) == ("30.00", "Per person")
    r = desk.post(
        f"{CHECKOUT}{pay.json()['id']}/confirm/",
        {"pin": PIN},
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert r.status_code == 201, r.json()
    assert balance(world) == Decimal("70.00")
    assert Booking.objects.get().court_price_id == person


@pytest.mark.django_db
def test_the_first_price_is_the_default(desk, world, two_prices):  # noqa: F811
    booking = reserve(desk, world, tomorrow_at(14), people=3).json()
    # Whole court: people do not count.
    assert (booking["price"], booking["price_name"], booking["people"]) == (
        "40.00",
        "Whole court",
        1,
    )


@pytest.mark.django_db
def test_a_price_of_another_court_is_refused(desk, world, two_prices):  # noqa: F811
    other = Good.objects.create(
        service_position=world.playground.service_position,
        name="Other court",
        kind="RENTAL",
        price="1.00",
        track_stock=False,
    )
    stranger = CourtPrice.objects.create(good=other, name="X", price="1.00")
    r = reserve(desk, world, tomorrow_at(14), price=stranger.pk)
    assert r.json()["error"]["code"] == "INVALID_SLOT"


@pytest.mark.django_db
def test_change_and_remove_prices(desk, world, two_prices):  # noqa: F811
    whole, person = two_prices
    url = f"{GOODS}{world.playground.pk}/prices/"
    # The court's own price follows its first price.
    assert desk.patch(f"{url}{whole}/", {"price": "25.00"}).status_code == 200
    world.playground.refresh_from_db()
    assert world.playground.price == Decimal("25.00")

    # A used price is switched off, not deleted; an unused one is deleted.
    reserve(desk, world, tomorrow_at(14), price=person)
    assert desk.delete(f"{url}{person}/").status_code == 204
    assert CourtPrice.objects.get(pk=person).is_active is False
    extra = add(desk, world, "Evening", "30.00").json()["id"]
    assert desk.delete(f"{url}{extra}/").status_code == 204
    assert not CourtPrice.objects.filter(pk=extra).exists()

    # The last price stays.
    assert desk.delete(f"{url}{whole}/").json()["error"]["code"] == "LAST_COURT_PRICE"
    assert [p.pk for p in prices(world)] == [whole]
