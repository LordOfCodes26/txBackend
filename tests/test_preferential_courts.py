from decimal import Decimal

import pytest

from apps.bookings.models import Booking
from apps.finance import services as finance
from apps.purchases.models import PurchaseItem
from tests import test_bookings
from tests.test_bookings import (
    BOOKINGS,
    balance,
    book,
    tomorrow_at,
)
from tests.test_preferential import spent_last_month, turn_on

desk = test_bookings.desk
manual_card_entry = test_bookings.manual_card_entry
world = test_bookings.world


@pytest.mark.django_db
def test_free_court_slots_stay_free_when_the_booking_moves(desk, world):
    spent_last_month(world.account, "150.00")
    turn_on(world.playground, items=1)
    before = balance(world)
    r = book(desk, world, tomorrow_at(10), slots=2)
    assert r.status_code == 201, r.json()
    assert r.json()["total"] == "20.00"  # 1 of 2 slots free
    assert PurchaseItem.objects.get().free_quantity == 1
    booking = Booking.objects.get()

    day = tomorrow_at(0).date().isoformat()

    def move(start, end):
        body = {"date": day, "start_time": start, "end_time": end}
        r = desk.post(f"{BOOKINGS}{booking.pk}/change/", body)
        assert r.status_code == 200, r.json()

    move("14:00", "16:00")  # same length: the free slot stays free, no money moves
    assert (balance(world), PurchaseItem.objects.get().line_total) == (
        before - Decimal("20.00"),
        Decimal("20.00"),
    )
    move("14:00", "15:00")  # only the free slot left: what was paid comes back
    assert (balance(world), PurchaseItem.objects.get().line_total) == (before, Decimal("0.00"))
    assert finance.ledger_mismatches() == []
