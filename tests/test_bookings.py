import threading
import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.accounts.rbac import Roles
from apps.audit.models import AuditLog
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
from apps.seller_finance.services import seller_ledger_mismatches
from apps.sellers.models import Seller, ServicePosition

GOODS = "/api/v1/goods/"
RENTALS = "/api/v1/rentals/"
BOOKINGS = "/api/v1/bookings/"
PURCHASES = "/api/v1/purchases/"
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


def add_booking(client, world, start, slots=1, good=None, purchase=None):
    """Add a booking line to a (new) draft; returns (purchase id, response)."""
    if purchase is None:
        purchase = client.post(PURCHASES, {"service_position": world.position.pk}).json()["id"]
    response = client.post(
        f"{PURCHASES}{purchase}/items/",
        {"good": (good or world.playground).pk, **times(start, slots)},
    )
    return purchase, response


def confirm(client, purchase, pin=PIN, uid=UID, idem=None):
    return client.post(
        f"{PURCHASES}{purchase}/confirm/",
        {"card_uid": uid, "pin": pin},
        HTTP_IDEMPOTENCY_KEY=idem or key(),
    )


def book(client, world, start, slots=1, pin=PIN, idem=None, good=None, uid=UID):
    """The whole desk flow: draft, booking line, card + PIN. Returns the first error
    response, or the confirmation response."""
    purchase, response = add_booking(client, world, start, slots, good)
    if response.status_code != 200:
        return response
    return confirm(client, purchase, pin=pin, uid=uid, idem=idem)


def draft(world, good, start, slots=1):
    """A draft purchase with one booking line, prepared at the desk (service level)."""
    purchase = purchases.create_purchase(actor=world.seller_user, service_position=world.position)
    local = timezone.localtime(start)
    end = (local + timedelta(hours=slots)).time()
    purchases.add_item(
        purchase=purchase, good=good, date=local.date(), start_time=local.time(), end_time=end
    )
    return purchase


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
def test_rentals_are_added_like_goods_with_date_and_times(auth_client, world):
    client = auth_client(world.seller_user)
    pid = client.post(PURCHASES, {"service_position": world.position.pk}).json()["id"]
    response = client.post(f"{PURCHASES}{pid}/items/", {"good": world.playground.pk})
    assert response.status_code == 400
    assert set(response.json()["error"]["details"]) == {"date", "start_time", "end_time"}
    ball = Good.objects.create(
        service_position=world.position, name="Ball", price="5.00", track_stock=False
    )
    response = client.post(f"{PURCHASES}{pid}/items/", {"good": ball.pk, "start_time": "10:00"})
    assert "start_time" in response.json()["error"]["details"]
    day = tomorrow_at(0).date().isoformat()
    response = client.post(
        f"{PURCHASES}{pid}/items/",
        {"good": world.playground.pk, "date": day, "start_time": "12:00", "end_time": "10:00"},
    )
    assert "end_time" in response.json()["error"]["details"]
    response = client.post(
        f"{PURCHASES}{pid}/items/",
        {"good": world.playground.pk, "date": day, "start_time": "10:00", "end_time": "11:30"},
    )
    assert response.json()["error"]["code"] == "INVALID_SLOT"
    assert response.json()["error"]["details"] == {"slot_minutes": 60}

    # Goods and a court in one purchase: 10:00-12:00 = 2 slots of 60 minutes.
    client.post(f"{PURCHASES}{pid}/items/", {"good": ball.pk, "quantity": 2})
    response = client.post(
        f"{PURCHASES}{pid}/items/",
        {"good": world.playground.pk, "date": day, "start_time": "10:00", "end_time": "12:00"},
    )
    items = response.json()["items"]
    assert [(i["good_name"], i["quantity"], i["line_total"]) for i in items] == [
        ("Ball", 2, "10.00"),
        ("Playground", 2, "40.00"),
    ]
    assert (items[1]["date"], items[1]["start_time"], items[1]["end_time"]) == (
        day,
        "10:00",
        "12:00",
    )
    assert items[0]["date"] is None
    assert response.json()["total"] == "50.00"


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
def test_booking_charges_developer_and_credits_seller(desk, world):
    pid, draft = add_booking(desk, world, tomorrow_at(14), slots=2)
    assert draft.status_code == 200, draft.json()
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
    assert world.seller.account.balance == Decimal("40.00")
    assert AuditLog.objects.filter(action="booking.created").exists()
    assert finance.ledger_mismatches() == [] and seller_ledger_mismatches() == []


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
    assert finance.open_account(bob).balance == Decimal("50.00")


@pytest.mark.django_db
def test_changing_the_time_rechecks_the_rules(desk, world):
    pid, draft = add_booking(desk, world, tomorrow_at(10))
    item = f"{PURCHASES}{pid}/items/{draft.json()['items'][0]['id']}/"
    r = desk.patch(item, {"end_time": "14:00"})  # 4 slots
    assert r.json()["error"]["details"] == {"max_slots_per_booking": 3}
    r = desk.patch(item, {"end_time": "13:00"})
    line = r.json()["items"][0]
    assert (line["start_time"], line["end_time"], line["line_total"]) == ("10:00", "13:00", "60.00")
    r = desk.patch(item, {"start_time": "15:00", "end_time": "16:00"})
    assert (r.json()["items"][0]["start_time"], r.json()["items"][0]["quantity"]) == ("15:00", 1)
    assert desk.patch(item, {"quantity": 2}).status_code == 400
    # Adding the same court again replaces its time.
    _, again = add_booking(desk, world, tomorrow_at(8), purchase=pid)
    assert [(i["start_time"], i["end_time"]) for i in again.json()["items"]] == [("08:00", "09:00")]


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
    assert r.json()["error"]["code"] == "GOOD_NOT_AVAILABLE"


@pytest.mark.django_db
def test_booking_with_a_tapped_card(desk, world, settings):
    """Production flow: no typed UID; the developer taps the card on the desk's reader."""
    settings.PURCHASE_ALLOW_MANUAL_CARD_UID = False
    reader, _key = rfid.register_device(actor=None, code="Reader1", purpose="TILL")
    pid = desk.post(PURCHASES, {"service_position": world.position.pk, "reader": "Reader1"}).json()[
        "id"
    ]
    add_booking(desk, world, tomorrow_at(10), purchase=pid)

    r = desk.post(f"{PURCHASES}{pid}/confirm/", {"pin": PIN}, HTTP_IDEMPOTENCY_KEY=key())
    assert r.json()["error"]["code"] == "CARD_NOT_PRESENTED"

    rfid.record_scan(device=reader, uid=UID)
    assert (
        desk.get(f"{PURCHASES}{pid}/").json()["presented_card"]["developer"]["full_name"] == "Ada"
    )
    r = desk.post(f"{PURCHASES}{pid}/confirm/", {"pin": PIN}, HTTP_IDEMPOTENCY_KEY=key())
    assert r.status_code == 201, r.json()
    assert Booking.objects.get().developer == world.developer


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
    assert auth_client(make_user(Roles.SELLER_MANAGER)).get(BOOKINGS).json()["count"] == 1


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
    assert finance.ledger_mismatches() == [] and seller_ledger_mismatches() == []


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
