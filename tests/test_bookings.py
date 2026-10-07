import threading
import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.accounts.rbac import Roles
from apps.audit.models import AuditLog
from apps.bookings import services as bookings
from apps.bookings.exceptions import SlotUnavailable
from apps.bookings.models import Booking
from apps.developers.models import Developer
from apps.finance import services as finance
from apps.finance.models import DeveloperAccount
from apps.goods.models import Good, GoodKind, RentalSettings
from apps.purchases import services as purchases
from apps.purchases.models import Purchase
from apps.rfid import services as rfid
from apps.rfid.models import RFIDCard, RFIDCardAssignment
from apps.sellers.models import Seller, ServicePosition

GOODS = "/api/v1/goods/"
RENTALS = "/api/v1/rentals/"
BOOKINGS = "/api/v1/bookings/"
PURCHASES = "/api/v1/purchases/"
CHECKOUT = "/api/v1/bookings/checkout/"
PIN = "4826"
UID = "04AABBCCDD"


@pytest.fixture(autouse=True)
def manual_card_entry(settings):
    """Most tests confirm with a typed card UID; test_booking_with_a_tapped_card covers the
    reader flow used in production."""
    settings.PURCHASE_ALLOW_MANUAL_CARD_UID = True


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
    developer = Developer.objects.create(employee_number="E1", full_name="Ada")
    RFIDCardAssignment.objects.create(card=RFIDCard.objects.create(uid=UID), developer=developer)
    account = finance.open_account(developer)
    finance.deposit(actor=None, developer=developer, amount="100.00", idempotency_key="seed-0001")
    finance.set_pin(actor=None, account=account, pin=PIN, current_pin=None)

    class W:
        pass

    w = W()
    w.__dict__.update(locals())
    return w


@pytest.fixture
def desk(auth_client, world):
    """The playground desk: the seller's staff, signed in. Developers never sign in."""
    return auth_client(world.seller_user)


def times(start, slots=1, slot_minutes=60):
    """The desk's input: company-local date, start time and end time."""
    local = timezone.localtime(start)
    end = local + timedelta(minutes=slot_minutes * slots)
    return {"date": local.date(), "start_time": f"{local:%H:%M}", "end_time": f"{end:%H:%M}"}


def add_booking(client, world, start, slots=1, good=None, **extra):
    """Start a booking checkout at the desk; returns (checkout id or None, response)."""
    response = client.post(
        CHECKOUT, {"good": (good or world.playground).pk, **times(start, slots), **extra}
    )
    return response.json().get("id"), response


def confirm(client, purchase, pin=PIN, uid=UID, idem=None):
    return client.post(
        f"{CHECKOUT}{purchase}/confirm/",
        {"card_uid": uid, "pin": pin},
        HTTP_IDEMPOTENCY_KEY=idem or key(),
    )


def book(client, world, start, slots=1, pin=PIN, idem=None, good=None, uid=UID):
    """The whole desk flow: draft, booking line, card + PIN. Returns the first error
    response, or the confirmation response."""
    purchase, response = add_booking(client, world, start, slots, good)
    if response.status_code != 201:
        return response
    return confirm(client, purchase, pin=pin, uid=uid, idem=idem)


def draft(world, good, start, slots=1):
    """A booking checkout prepared at the desk (service level)."""
    local = timezone.localtime(start)
    return bookings.start_checkout(
        actor=world.seller_user,
        good=good,
        day=local.date(),
        start_time=local.time(),
        end_time=(local + timedelta(hours=slots)).time(),
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


# --- Browsing and availability ----------------------------------------------------------


@pytest.mark.django_db
def test_developer_browses_rentals_and_availability(desk, world):
    rentals = desk.get(RENTALS).json()["results"]
    assert [r["name"] for r in rentals] == ["Playground"]
    assert rentals[0]["rental"]["slot_minutes"] == 60

    day = tomorrow_at(0).date()
    slots = desk.get(f"{RENTALS}{world.playground.pk}/availability/?date={day}").json()
    assert len(slots) == 12  # 08:00-20:00 in 1h slots
    assert all(s["available"] for s in slots)

    book(desk, world, tomorrow_at(10), slots=2)
    slots = desk.get(f"{RENTALS}{world.playground.pk}/availability/?date={day}").json()
    taken = [
        timezone.localtime(datetime.fromisoformat(s["start"])).hour
        for s in slots
        if not s["available"]
    ]
    assert taken == [10, 11]


@pytest.mark.django_db
def test_day_schedule_shows_booked_and_free_times_of_every_court(desk, world):
    tennis = Good.objects.create(
        service_position=world.position,
        name="Tennis court",
        price="15.00",
        kind=GoodKind.RENTAL,
        track_stock=False,
    )
    RentalSettings.objects.create(
        good=tennis, slot_minutes=30, opening_time=time(9), closing_time=time(12)
    )
    book(desk, world, tomorrow_at(10), slots=2)  # Playground 10-12
    book(desk, world, tomorrow_at(15))  # Playground 15-16

    day = tomorrow_at(0).date().isoformat()
    body = desk.get(f"{RENTALS}schedule/?date={day}").json()
    assert body["date"] == day
    courts = {c["name"]: c for c in body["courts"]}
    assert set(courts) == {"Playground", "Tennis court"}

    playground = courts["Playground"]
    assert (playground["open"], playground["slot_minutes"]) == (True, 60)
    assert [(p["state"], p["start_time"], p["end_time"]) for p in playground["periods"]] == [
        ("FREE", "08:00", "10:00"),
        ("BOOKED", "10:00", "12:00"),
        ("FREE", "12:00", "15:00"),
        ("BOOKED", "15:00", "16:00"),
        ("FREE", "16:00", "20:00"),
    ]
    assert [
        (p["state"], p["start_time"], p["end_time"]) for p in courts["Tennis court"]["periods"]
    ] == [("FREE", "09:00", "12:00")]
    # Who booked is not shown, only the times.
    assert "developer" not in str(body)

    only = desk.get(f"{RENTALS}schedule/?date={day}&search=tennis").json()["courts"]
    assert [c["name"] for c in only] == ["Tennis court"]
    assert desk.get(f"{RENTALS}schedule/").status_code == 400


@pytest.mark.django_db
def test_schedule_marks_past_and_closed_days(desk, world):
    today = timezone.localdate()
    RentalSettings.objects.filter(good=world.playground).update(
        opening_time=time(0), closing_time=time(23)
    )
    periods = desk.get(f"{RENTALS}schedule/?date={today}").json()["courts"][0]["periods"]
    assert periods[0]["state"] == "PAST"
    if timezone.localtime().hour < 22:  # late in the day every slot is already past
        assert periods[-1]["state"] == "FREE"

    far = today + timedelta(days=20)
    periods = desk.get(f"{RENTALS}schedule/?date={far}").json()["courts"][0]["periods"]
    assert {p["state"] for p in periods} == {"NOT_YET_OPEN"}

    RentalSettings.objects.filter(good=world.playground).update(
        weekdays=[d for d in range(7) if d != far.weekday()]
    )
    court = desk.get(f"{RENTALS}schedule/?date={far}").json()["courts"][0]
    assert (court["open"], court["periods"]) == (False, [])


@pytest.mark.django_db
def test_inactive_rental_is_hidden(desk, world):
    Good.objects.filter(pk=world.playground.pk).update(is_active=False)
    assert desk.get(RENTALS).json()["count"] == 0
    assert book(desk, world, tomorrow_at(10)).json()["error"]["code"] == "RENTAL_NOT_AVAILABLE"


@pytest.mark.django_db
def test_closed_weekday_has_no_slots(desk, world):
    day = tomorrow_at(0).date()
    RentalSettings.objects.filter(good=world.playground).update(
        weekdays=[d for d in range(7) if d != day.weekday()]
    )
    assert desk.get(f"{RENTALS}{world.playground.pk}/availability/?date={day}").json() == []
    assert book(desk, world, tomorrow_at(10)).json()["error"]["code"] == "INVALID_SLOT"


# --- Booking --------------------------------------------------------------------------


@pytest.mark.django_db
def test_booking_charges_developer(desk, world):
    pid, draft = add_booking(desk, world, tomorrow_at(14), slots=2)
    assert draft.status_code == 201, draft.json()
    assert draft.json()["kind"] == "BOOKING"
    line = draft.json()["items"][0]
    assert (line["kind"], line["quantity"], line["line_total"]) == ("RENTAL", 2, "40.00")
    assert timezone.localtime(datetime.fromisoformat(line["end"])).hour == 16
    assert draft.json()["total"] == "40.00"

    response = confirm(desk, pid)
    assert response.status_code == 201, response.json()
    assert (response.json()["total"], response.json()["balance_after"]) == ("40.00", "60.00")
    booking = Booking.objects.get()
    assert (booking.developer, booking.slots, booking.purchase_id) == (world.developer, 2, pid)
    assert timezone.localtime(booking.end).hour == 16
    body = desk.get(f"{BOOKINGS}{booking.pk}/").json()
    assert (body["total"], body["balance_after"], body["slots"]) == ("40.00", "60.00", 2)
    assert (body["start_time"], body["end_time"]) == ("14:00", "16:00")

    assert balance(world) == Decimal("60.00")
    purchase = Purchase.objects.get(pk=pid)
    assert (purchase.status, purchase.total, purchase.developer_id) == (
        "CONFIRMED",
        Decimal("40.00"),
        world.developer.pk,
    )
    assert AuditLog.objects.filter(action="booking.created").exists()
    assert finance.ledger_mismatches() == []


@pytest.mark.django_db
def test_overlapping_booking_is_rejected(desk, world):
    assert book(desk, world, tomorrow_at(10), slots=2).status_code == 201  # 10-12
    r = book(desk, world, tomorrow_at(11))
    assert (r.status_code, r.json()["error"]["code"]) == (409, "SLOT_UNAVAILABLE")
    assert book(desk, world, tomorrow_at(12)).status_code == 201  # back-to-back is fine
    assert balance(world) == Decimal("40.00")


@pytest.mark.django_db
def test_slot_taken_while_the_developer_enters_the_pin(desk, world):
    """Two desks prepare the same slot: the first to confirm gets it, nothing is charged
    for the second."""
    first, _ = add_booking(desk, world, tomorrow_at(10))
    second, _ = add_booking(desk, world, tomorrow_at(10))
    bob = Developer.objects.create(employee_number="E2", full_name="Bob")
    RFIDCardAssignment.objects.create(card=RFIDCard.objects.create(uid="04BB"), developer=bob)
    finance.deposit(actor=None, developer=bob, amount="50.00", idempotency_key="seed-0002")
    finance.set_pin(actor=None, account=finance.open_account(bob), pin="5082", current_pin=None)

    assert confirm(desk, first).status_code == 201
    r = confirm(desk, second, pin="5082", uid="04BB")
    assert (r.status_code, r.json()["error"]["code"]) == (409, "SLOT_UNAVAILABLE")
    assert Purchase.objects.get(pk=second).status == "DRAFT"
    assert desk.post(f"{CHECKOUT}{second}/cancel/").json()["status"] == "CANCELLED"
    assert finance.open_account(bob).balance == Decimal("50.00")


@pytest.mark.django_db
def test_only_rentals_of_the_own_seller_can_be_booked(desk, world, make_user):
    other = Seller.objects.create(name="Other", user=make_user(Roles.SELLER))
    position = ServicePosition.objects.create(seller=other, name="Pool")
    pool = Good.objects.create(
        service_position=position,
        name="Pool",
        price="5.00",
        kind=GoodKind.RENTAL,
        track_stock=False,
    )
    RentalSettings.objects.create(
        good=pool, slot_minutes=60, opening_time=time(8), closing_time=time(20)
    )
    _, r = add_booking(desk, world, tomorrow_at(10), good=pool)
    assert r.json()["error"]["details"] == {"good": ["You can only book your own courts."]}


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
def test_invalid_slots(desk, world, start_fn):
    response = book(desk, world, start_fn(), slots=2)
    assert response.json()["error"]["code"] == "INVALID_SLOT", response.json()
    assert balance(world) == Decimal("100.00")


@pytest.mark.django_db
def test_too_many_slots(desk, world):
    r = book(desk, world, tomorrow_at(9), slots=4)
    assert r.json()["error"]["details"] == {"max_slots_per_booking": 3}


@pytest.mark.django_db
def test_booking_needs_pin_and_balance(desk, world):
    assert book(desk, world, tomorrow_at(9), pin="0000").json()["error"]["code"] == "INVALID_PIN"
    Good.objects.filter(pk=world.playground.pk).update(price="60.00")
    r = book(desk, world, tomorrow_at(9), slots=2)
    assert r.json()["error"]["code"] == "INSUFFICIENT_BALANCE"
    assert not Booking.objects.exists()
    assert not Purchase.objects.filter(status="CONFIRMED").exists()


@pytest.mark.django_db
def test_booking_retry_is_idempotent(desk, world):
    idem = key()
    pid, _ = add_booking(desk, world, tomorrow_at(9))
    first = confirm(desk, pid, idem=idem)
    again = confirm(desk, pid, idem=idem)
    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json()["id"] == again.json()["id"]
    assert Booking.objects.count() == 1
    assert balance(world) == Decimal("80.00")
    other = book(desk, world, tomorrow_at(15), idem=idem)
    assert other.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.django_db
def test_database_rejects_overlap_directly(world, desk):
    book(desk, world, tomorrow_at(10))
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
def test_who_sees_bookings(auth_client, make_user, world, desk):
    book(desk, world, tomorrow_at(9))
    assert auth_client(make_user(Roles.DEVELOPER)).get(BOOKINGS).status_code == 403

    seller_view = auth_client(world.seller_user).get(BOOKINGS).json()["results"]
    assert [b["developer"]["full_name"] for b in seller_view] == ["Ada"]

    other = make_user(Roles.SELLER)
    Seller.objects.create(name="Other", user=other)
    assert auth_client(other).get(BOOKINGS).json()["count"] == 0
    assert auth_client(make_user(Roles.ADMIN)).get(BOOKINGS).json()["count"] == 1


# --- Concurrency -------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_two_developers_race_for_the_same_slot(world, make_user):
    dev2 = Developer.objects.create(employee_number="E2", full_name="Bob")
    RFIDCardAssignment.objects.create(card=RFIDCard.objects.create(uid="04BB"), developer=dev2)
    acc2 = finance.open_account(dev2)
    finance.deposit(actor=None, developer=dev2, amount="100.00", idempotency_key="seed-0002")
    finance.set_pin(actor=None, account=acc2, pin="5082", current_pin=None)
    start = tomorrow_at(10)

    barrier = threading.Barrier(2)
    outcomes = []

    drafts = [draft(world, world.playground, start) for _ in range(2)]

    def attempt(purchase, uid, pin):
        try:
            barrier.wait()
            outcomes.append(
                purchases.confirm_purchase(
                    actor=None, purchase=purchase, pin=pin, idempotency_key=key(), card_uid=uid
                )
            )
        except SlotUnavailable as exc:
            outcomes.append(exc)
        finally:
            connection.close()

    threads = [
        threading.Thread(target=attempt, args=a)
        for a in ((drafts[0], UID, PIN), (drafts[1], "04BB", "5082"))
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
    assert finance.ledger_mismatches() == []


# --- Gaps found in review ------------------------------------------------------------------


@pytest.mark.django_db
def test_daily_limit_cannot_be_bypassed_with_several_bookings(desk, world):
    RentalSettings.objects.filter(good=world.playground).update(max_slots_per_day=4)
    assert book(desk, world, tomorrow_at(8), slots=3).status_code == 201
    r = book(desk, world, tomorrow_at(11), slots=2)
    assert r.status_code == 409
    assert r.json()["error"] == {
        "code": "DAILY_LIMIT_REACHED",
        "message": "You have reached today's booking limit for this rental.",
        "details": {"max_slots_per_day": 4, "already_booked": 3, "remaining": 1},
    }
    assert book(desk, world, tomorrow_at(11), slots=1).status_code == 201
    assert book(desk, world, tomorrow_at(15), slots=1).status_code == 409
    # The limit is per day: the day after is fine.
    assert book(desk, world, tomorrow_at(8) + timedelta(days=1)).status_code == 201


@pytest.mark.django_db
def test_daily_limit_is_per_developer(desk, auth_client, make_user, world):
    RentalSettings.objects.filter(good=world.playground).update(max_slots_per_day=1)
    assert book(desk, world, tomorrow_at(8)).status_code == 201
    other = Developer.objects.create(employee_number="E2", full_name="Bob")
    RFIDCardAssignment.objects.create(card=RFIDCard.objects.create(uid="04BB"), developer=other)
    acc = finance.open_account(other)
    finance.deposit(actor=None, developer=other, amount="50.00", idempotency_key="seed-0002")
    finance.set_pin(actor=None, account=acc, pin="5082", current_pin=None)
    assert book(desk, world, tomorrow_at(9), pin="5082", uid="04BB").status_code == 201


@pytest.mark.django_db
def test_rental_without_rules_is_hidden_and_not_bookable(desk, world):
    bare = Good.objects.create(
        service_position=world.position,
        name="Room",
        price="5.00",
        kind=GoodKind.RENTAL,
        track_stock=False,
    )
    assert [r["name"] for r in desk.get(RENTALS).json()["results"]] == ["Playground"]
    assert desk.get(f"{RENTALS}{bare.pk}/availability/?date=2026-10-02").status_code == 404
    r = book(desk, world, tomorrow_at(9), good=bare)
    assert r.json()["error"]["code"] == "RENTAL_NOT_AVAILABLE"


@pytest.mark.django_db
def test_rentals_need_a_positive_price(auth_client, world):
    client = auth_client(world.seller_user)
    rules = {"slot_minutes": 60, "opening_time": "08:00", "closing_time": "20:00"}
    r = client.post(
        GOODS,
        {
            "service_position": world.position.pk,
            "name": "Free",
            "price": "0.00",
            "kind": "RENTAL",
            "rental": rules,
        },
        format="json",
    )
    assert "price" in r.json()["error"]["details"]
    r = client.patch(f"{GOODS}{world.playground.pk}/", {"price": "0"}, format="json")
    assert "price" in r.json()["error"]["details"]


@pytest.mark.django_db
def test_one_developer_cannot_hold_two_courts_at_once(desk, world):
    other_court = Good.objects.create(
        service_position=world.position,
        name="Tennis court",
        price="15.00",
        kind=GoodKind.RENTAL,
        track_stock=False,
    )
    RentalSettings.objects.create(
        good=other_court,
        slot_minutes=60,
        opening_time=time(8),
        closing_time=time(20),
        max_slots_per_booking=3,
    )
    first = book(desk, world, tomorrow_at(10), slots=2)  # Playground 10-12
    assert first.status_code == 201

    r = book(desk, world, tomorrow_at(11), good=other_court)  # overlaps 11-12
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "ALREADY_BOOKED_THEN"
    assert (err["details"]["booking"], err["details"]["good"]) == (
        Booking.objects.get().pk,
        "Playground",
    )
    # Right after is fine.
    assert book(desk, world, tomorrow_at(12), good=other_court).status_code == 201


@pytest.mark.django_db
def test_database_blocks_overlapping_bookings_of_one_developer(desk, world):
    book(desk, world, tomorrow_at(10))
    existing = Booking.objects.get()
    court = Good.objects.create(
        service_position=world.position,
        name="Court",
        price="5.00",
        kind=GoodKind.RENTAL,
        track_stock=False,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        Booking.objects.create(
            good=court,
            developer=world.developer,
            purchase=existing.purchase,
            start=tomorrow_at(10),
            end=tomorrow_at(11),
            slots=1,
        )


@pytest.mark.django_db(transaction=True)
def test_same_developer_racing_for_two_courts_gets_one(world):
    court = Good.objects.create(
        service_position=world.position,
        name="Tennis court",
        price="15.00",
        kind=GoodKind.RENTAL,
        track_stock=False,
    )
    RentalSettings.objects.create(
        good=court,
        slot_minutes=60,
        opening_time=time(8),
        closing_time=time(20),
        max_slots_per_booking=3,
    )
    start = tomorrow_at(10)
    barrier = threading.Barrier(2)
    outcomes = []

    drafts = [draft(world, g, start) for g in (world.playground, court)]

    def attempt(purchase):
        try:
            barrier.wait()
            outcomes.append(
                purchases.confirm_purchase(
                    actor=None, purchase=purchase, pin=PIN, idempotency_key=key(), card_uid=UID
                )
            )
        except Exception as exc:  # noqa: BLE001
            outcomes.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt, args=(d,)) for d in drafts]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(isinstance(o, tuple) for o in outcomes) == 1, outcomes
    assert [getattr(o, "code", None) for o in outcomes if not isinstance(o, tuple)] == [
        "ALREADY_BOOKED_THEN"
    ]
    winner = Booking.objects.get()  # either court may win the race
    assert balance(world) == Decimal("100.00") - winner.good.price  # charged exactly once


# --- Split from goods -------------------------------------------------------------------


@pytest.mark.django_db
def test_courts_and_goods_never_share_a_purchase(desk, world):
    ball = Good.objects.create(
        service_position=world.position, name="Ball", price="5.00", track_stock=False
    )
    sale = desk.post(PURCHASES, {"service_position": world.position.pk}).json()
    assert sale["kind"] == "SALE"
    r = desk.post(f"{PURCHASES}{sale['id']}/items/", {"good": world.playground.pk})
    assert r.json()["error"]["code"] == "GOOD_NOT_AVAILABLE"

    checkout, _ = add_booking(desk, world, tomorrow_at(10))
    r = desk.post(f"{PURCHASES}{checkout}/items/", {"good": ball.pk})
    assert r.json()["error"]["code"] == "GOOD_NOT_AVAILABLE"
    item = Purchase.objects.get(pk=checkout).items.get()
    assert desk.delete(f"{PURCHASES}{checkout}/items/{item.pk}/").status_code == 409
    # Checkouts only hold courts; till purchases aren't visible there.
    assert desk.get(f"{CHECKOUT}{sale['id']}/").status_code == 404
    assert [p["kind"] for p in desk.get(f"{PURCHASES}?kind=BOOKING").json()["results"]] == [
        "BOOKING"
    ]


@pytest.mark.django_db
def test_checkout_input_validation(desk, world):
    day = tomorrow_at(0).date().isoformat()
    r = desk.post(CHECKOUT, {"good": world.playground.pk})
    assert set(r.json()["error"]["details"]) == {"date", "start_time", "end_time"}
    r = desk.post(
        CHECKOUT,
        {"good": world.playground.pk, "date": day, "start_time": "12:00", "end_time": "10:00"},
    )
    assert "end_time" in r.json()["error"]["details"]
    r = desk.post(
        CHECKOUT,
        {"good": world.playground.pk, "date": day, "start_time": "10:00", "end_time": "11:30"},
    )
    assert (r.json()["error"]["code"], r.json()["error"]["details"]) == (
        "INVALID_SLOT",
        {"slot_minutes": 60},
    )
    ball = Good.objects.create(
        service_position=world.position, name="Ball", price="5.00", track_stock=False
    )
    r = desk.post(
        CHECKOUT, {"good": ball.pk, "date": day, "start_time": "10:00", "end_time": "11:00"}
    )
    assert "good" in r.json()["error"]["details"]


@pytest.mark.django_db
def test_booking_with_a_tapped_card(desk, world, settings):
    """Production flow: no typed UID; the developer taps the card on the desk's reader."""
    settings.PURCHASE_ALLOW_MANUAL_CARD_UID = False
    reader, _key = rfid.register_device(
        actor=None, code="Reader1", purpose="TILL", seller=world.position.seller
    )
    pid, r = add_booking(desk, world, tomorrow_at(10), reader="Reader1")
    assert r.json()["reader"] == "Reader1"
    rfid.record_scan(device=reader, uid=UID)  # before Scan card to book: not taken
    assert desk.get(f"{CHECKOUT}{pid}/").json()["presented_card"] is None
    assert desk.post(f"{CHECKOUT}{pid}/wait/").json()["waiting_for_card"] is True

    r = desk.post(f"{CHECKOUT}{pid}/confirm/", {"pin": PIN}, HTTP_IDEMPOTENCY_KEY=key())
    assert r.json()["error"]["code"] == "CARD_NOT_PRESENTED"

    rfid.record_scan(device=reader, uid=UID)
    assert desk.get(f"{CHECKOUT}{pid}/").json()["presented_card"]["developer"]["full_name"] == "Ada"
    r = desk.post(f"{CHECKOUT}{pid}/confirm/", {"pin": PIN}, HTTP_IDEMPOTENCY_KEY=key())
    assert r.status_code == 201, r.json()
    assert Booking.objects.get().developer == world.developer


# --- Changing a paid booking -------------------------------------------------------------


def change(client, booking, **body):
    return client.post(f"{BOOKINGS}{booking}/change/", body)


@pytest.fixture
def paid(desk, world):
    """Ada's paid booking: Playground tomorrow 10:00-12:00 (2 x 20.00)."""
    assert book(desk, world, tomorrow_at(10), slots=2).status_code == 201
    return Booking.objects.get()


@pytest.mark.django_db
def test_change_booking_time(desk, world, paid):
    day = tomorrow_at(0).date().isoformat()
    r = change(desk, paid.pk, date=day, start_time="14:00", end_time="16:00")
    assert r.status_code == 200, r.json()
    body = r.json()
    assert (body["start_time"], body["end_time"], body["change_count"]) == ("14:00", "16:00", 1)
    slots = desk.get(f"{RENTALS}{world.playground.pk}/availability/?date={day}").json()
    assert [s["start_time"] for s in slots if s["state"] == "BOOKED"] == ["14:00", "15:00"]
    # No money moved.
    assert balance(world) == Decimal("60.00")
    assert finance.ledger_mismatches() == []
    log = AuditLog.objects.get(action="booking.changed")
    assert log.old_values["start"] == tomorrow_at(10).isoformat()
    # Overlapping its own old time is fine.
    r = change(desk, paid.pk, date=day, start_time="15:00", end_time="17:00")
    assert r.status_code == 200


@pytest.mark.django_db
def test_change_to_another_court(desk, world, paid):
    tennis = Good.objects.create(
        service_position=world.position,
        name="Tennis court",
        price="20.00",
        kind=GoodKind.RENTAL,
        track_stock=False,
    )
    RentalSettings.objects.create(
        good=tennis, slot_minutes=60, opening_time=time(8), closing_time=time(20)
    )
    day = tomorrow_at(0).date().isoformat()
    r = change(desk, paid.pk, good=tennis.pk, date=day, start_time="10:00", end_time="12:00")
    assert (r.status_code, r.json()["good_name"]) == (200, "Tennis court")
    # The playground is free again.
    assert book(desk, world, tomorrow_at(10), slots=1, uid=UID).json()["error"]["code"] == (
        "ALREADY_BOOKED_THEN"  # Ada holds the tennis court then
    )


@pytest.mark.django_db
def test_change_must_cost_the_same(desk, world, paid):
    day = tomorrow_at(0).date().isoformat()
    r = change(desk, paid.pk, date=day, start_time="14:00", end_time="15:00")
    assert r.status_code == 409
    assert r.json()["error"] == {
        "code": "BOOKING_PRICE_DIFFERENT",
        "message": "The new time must cost the same as the booking (same number of slots "
        "and price).",
        "details": {"paid": "40.00", "new_price": "20.00"},
    }


@pytest.mark.django_db
def test_change_follows_the_booking_rules(desk, world, paid):
    bob = Developer.objects.create(employee_number="E2", full_name="Bob")
    RFIDCardAssignment.objects.create(card=RFIDCard.objects.create(uid="04BB"), developer=bob)
    finance.deposit(actor=None, developer=bob, amount="50.00", idempotency_key="seed-0002")
    finance.set_pin(actor=None, account=finance.open_account(bob), pin="5082", current_pin=None)
    assert book(desk, world, tomorrow_at(14), slots=2, pin="5082", uid="04BB").status_code == 201

    day = tomorrow_at(0).date().isoformat()
    r = change(desk, paid.pk, date=day, start_time="15:00", end_time="17:00")
    assert r.json()["error"]["code"] == "SLOT_UNAVAILABLE"
    r = change(desk, paid.pk, date=day, start_time="10:30", end_time="12:30")
    assert r.json()["error"]["code"] == "INVALID_SLOT"
    paid.refresh_from_db()
    assert (paid.start, paid.change_count) == (tomorrow_at(10), 0)


@pytest.mark.django_db
def test_started_bookings_cannot_change(desk, world, paid):
    Booking.objects.filter(pk=paid.pk).update(
        start=timezone.now() - timedelta(minutes=5), end=timezone.now() + timedelta(minutes=55)
    )
    day = tomorrow_at(0).date().isoformat()
    r = change(desk, paid.pk, date=day, start_time="14:00", end_time="16:00")
    assert r.json()["error"]["code"] == "BOOKING_STARTED"


@pytest.mark.django_db
def test_only_the_courts_seller_changes_bookings(auth_client, make_user, world, paid):
    other = make_user(Roles.SELLER)
    Seller.objects.create(name="Other", user=other)
    day = tomorrow_at(0).date().isoformat()
    r = change(auth_client(other), paid.pk, date=day, start_time="14:00", end_time="16:00")
    assert r.status_code == 404
