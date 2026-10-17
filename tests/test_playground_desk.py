"""The playground desk: free bookings paid on arrival, "play now", per person prices, the
arrival tap, the gate for unpaid bookings, and the board of courts now."""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.bookings.models import Booking, CheckResult, PlaygroundCheck
from apps.finance.models import AccountTransaction
from apps.goods.models import RentalSettings
from apps.purchases.models import Purchase
from apps.rfid import services as rfid
from tests.test_bookings import PIN, UID, balance, desk, tomorrow_at, world  # noqa: F401

BOOKINGS = "/api/v1/bookings/"
CHECKOUT = "/api/v1/bookings/checkout/"


@pytest.fixture
def reader(world):  # noqa: F811
    """The desk's card assign reader (taps for arrivals and payments)."""
    device, _key = rfid.register_device(actor=None, code="DESK1", purpose="ENROLL")
    return device


def tap(device, uid=UID):
    event, _created = rfid.record_scan(device=device, uid=uid, client_event_id=str(uuid.uuid4()))
    return event


def reserve(desk, world, start, slots=2, people=1):  # noqa: F811
    local = timezone.localtime(start)
    end = local + timedelta(hours=slots)
    return desk.post(
        f"{BOOKINGS}reserve/",
        {
            "good": world.playground.pk,
            "developer": world.developer.pk,
            "date": local.date(),
            "start_time": f"{local:%H:%M}",
            "end_time": f"{end:%H:%M}",
            "people": people,
        },
    )


def confirm(desk, purchase_id, pin=PIN):  # noqa: F811
    return desk.post(
        f"{CHECKOUT}{purchase_id}/confirm/", {"pin": pin}, HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4())
    )


@pytest.fixture
def at_1020(monkeypatch):
    """Now is tomorrow 10:20 (company-local): the 10:00 slot is running. Use `same_day`
    for other times of that day (`tomorrow_at` would count from the new "now")."""
    moment = tomorrow_at(10, 20)
    monkeypatch.setattr(timezone, "now", lambda: moment)
    return moment


def same_day(moment, hour, minute=0):
    return timezone.localtime(moment).replace(hour=hour, minute=minute, second=0, microsecond=0)


@pytest.mark.django_db
def test_free_booking_then_pay_on_arrival_with_one_tap(desk, world, reader, at_1020):  # noqa: F811
    r = reserve(desk, world, same_day(at_1020, 14))
    assert r.status_code == 201, r.json()
    body = r.json()
    assert (body["status"], body["total"], body["price"]) == ("RESERVED", None, "40.00")
    assert balance(world) == Decimal("100.00")  # nothing paid yet
    assert not AccountTransaction.objects.filter(kind="PURCHASE").exists()

    # Arrival: the developer taps at the desk; the desk finds their booking.
    cursor = desk.get(f"{BOOKINGS}arrival/", {"reader": "DESK1"}).json()["cursor"] or 0
    event = tap(reader)
    seen = desk.get(f"{BOOKINGS}arrival/", {"reader": "DESK1", "after": cursor}).json()["tap"]
    assert seen["developer"]["full_name"] == "Ada"
    assert seen["now"] is None and [b["id"] for b in seen["later"]] == [body["id"]]  # later today

    # Pay: the arrival tap is the card; only the PIN is typed.
    r = desk.post(f"{BOOKINGS}{body['id']}/pay/", {"reader": "DESK1", "event": event.pk})
    assert r.status_code == 201, r.json()
    pay = r.json()
    assert pay["presented_card"]["developer"]["full_name"] == "Ada"
    assert pay["total"] == "40.00"
    r = confirm(desk, pay["id"])
    assert r.status_code == 201, r.json()
    booking = Booking.objects.get(pk=body["id"])
    assert (booking.status, booking.purchase_id) == ("PAID", pay["id"])
    assert booking.checked_in_at is not None  # paying at the desk checks in
    assert balance(world) == Decimal("60.00")
    assert Booking.objects.count() == 1  # paid, not booked twice


@pytest.mark.django_db
def test_gate_asks_an_unpaid_booking_to_pay_first(desk, world, at_1020):  # noqa: F811
    world.seller.checks_bookings = True
    world.seller.save()
    gate, _key = rfid.register_device(actor=None, code="GATE1", purpose="TILL", seller=world.seller)
    r = reserve(desk, world, same_day(at_1020, 11))
    assert r.status_code == 201, r.json()
    # Running now (10:00-12:00), not paid.
    Booking.objects.filter(pk=r.json()["id"]).update(
        start=same_day(at_1020, 10), end=same_day(at_1020, 12)
    )
    event = tap(gate)
    check = PlaygroundCheck.objects.get(event=event)
    assert check.result == CheckResult.NOT_PAID
    assert Booking.objects.get().checked_in_at is None


@pytest.mark.django_db
def test_only_the_developer_who_booked_pays(desk, world, reader, make_user):  # noqa: F811
    from apps.developers.models import Developer
    from apps.finance import services as finance
    from apps.rfid.models import RFIDCard, RFIDCardAssignment

    booking = reserve(desk, world, tomorrow_at(14)).json()
    bob = Developer.objects.create(employee_number="E2", full_name="Bob")
    RFIDCardAssignment.objects.create(card=RFIDCard.objects.create(uid="04BB"), developer=bob)
    finance.deposit(actor=None, developer=bob, amount="50.00", idempotency_key="bob-1")
    finance.set_pin(actor=None, account=finance.open_account(bob), pin=PIN, current_pin=None)
    pay = desk.post(
        f"{BOOKINGS}{booking['id']}/pay/", {"reader": "DESK1", "event": tap(reader, "04BB").pk}
    ).json()
    r = confirm(desk, pay["id"])
    assert r.json()["error"]["code"] == "CARD_NOT_USABLE"
    assert Booking.objects.get().status == "RESERVED"


@pytest.mark.django_db
def test_per_person_price(desk, world, reader):  # noqa: F811
    RentalSettings.objects.filter(good=world.playground).update(price_per="PERSON", max_people=6)
    assert reserve(desk, world, tomorrow_at(14), people=7).status_code == 400  # at most 6
    booking = reserve(desk, world, tomorrow_at(14), slots=2, people=4).json()
    assert (booking["people"], booking["price"]) == (4, "160.00")  # 20 x 2 slots x 4 people
    # Paying charges the same.
    pay = desk.post(
        f"{BOOKINGS}{booking['id']}/pay/", {"reader": "DESK1", "event": tap(reader).pk}
    ).json()
    assert pay["total"] == "160.00" and pay["items"][0]["people"] == 4


@pytest.mark.django_db
def test_play_now_starts_this_minute(desk, world, reader, at_1020):  # noqa: F811
    event = tap(reader)
    r = desk.post(
        CHECKOUT,
        {"good": world.playground.pk, "now_slots": 2, "reader": "DESK1", "event": event.pk},
    )
    assert r.status_code == 201, r.json()
    line = r.json()["items"][0]
    assert (line["start_time"], line["end_time"], line["line_total"]) == ("10:20", "12:20", "40.00")
    assert confirm(desk, r.json()["id"]).status_code == 201
    booking = Booking.objects.get()
    assert (booking.status, booking.checked_in_at is not None) == ("PAID", True)
    # The board shows the court in use until 12:20.
    court = desk.get(f"{BOOKINGS}now/").json()["courts"][0]
    assert (court["state"], court["until_time"], court["now"]["developer"]["full_name"]) == (
        "IN_USE",
        "12:20",
        "Ada",
    )


@pytest.mark.django_db
def test_board_free_until_next_booking(desk, world, at_1020):  # noqa: F811
    assert reserve(desk, world, same_day(at_1020, 15)).status_code == 201
    court = desk.get(f"{BOOKINGS}now/").json()["courts"][0]
    assert (court["state"], court["until_time"], court["play_slots"]) == ("FREE", "15:00", 4)
    assert (court["next"]["start_time"], court["next"]["status"]) == ("15:00", "RESERVED")


@pytest.mark.django_db
def test_cancel_and_move_a_reserved_booking(desk, world):  # noqa: F811
    booking = reserve(desk, world, tomorrow_at(14)).json()
    day = tomorrow_at(0).date().isoformat()
    r = desk.post(
        f"{BOOKINGS}{booking['id']}/change/",
        {"date": day, "start_time": "16:00", "end_time": "19:00"},
    )
    assert r.status_code == 200, r.json()
    assert (r.json()["start_time"], r.json()["status"], r.json()["price"]) == (
        "16:00",
        "RESERVED",
        "60.00",
    )
    assert balance(world) == Decimal("100.00")  # no money for an unpaid booking
    assert desk.post(f"{BOOKINGS}{booking['id']}/cancel/").status_code == 204
    assert not Booking.objects.exists()
    assert not Purchase.objects.exists()


@pytest.mark.django_db
def test_cancel_a_paid_booking_refunds_it(desk, world, reader):  # noqa: F811
    booking = reserve(desk, world, tomorrow_at(14)).json()
    pay = desk.post(
        f"{BOOKINGS}{booking['id']}/pay/", {"reader": "DESK1", "event": tap(reader).pk}
    ).json()
    confirm(desk, pay["id"])
    Booking.objects.filter(pk=booking["id"]).update(checked_in_at=None)  # not here yet
    assert balance(world) == Decimal("60.00")
    assert desk.post(f"{BOOKINGS}{booking['id']}/cancel/").status_code == 204
    assert balance(world) == Decimal("100.00")
    assert not Booking.objects.exists()


@pytest.mark.django_db
def test_the_desk_finds_who_a_booking_is_for(desk, world):  # noqa: F811
    """Playground sellers cannot list developers, but the desk finds them by name or number,
    with their team and wallet balance."""
    rows = desk.get(f"{BOOKINGS}people/", {"search": "ad"}).json()["results"]
    assert [(r["full_name"], r["balance"]) for r in rows] == [("Ada", "100.00")]
    one = desk.get(f"{BOOKINGS}people/", {"id": world.developer.pk}).json()["results"]
    assert one[0]["employee_number"] == world.developer.employee_number
    assert desk.get(f"{BOOKINGS}people/", {"search": " "}).json()["results"] == []
