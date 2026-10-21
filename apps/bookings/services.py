from datetime import date, datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.audit.services import record_audit
from apps.developers.models import Developer
from apps.finance.models import DeveloperAccount
from apps.goods.models import CourtPrice, Good, GoodKind, PricePer, RentalSettings
from apps.purchases.models import Purchase
from apps.sellers.models import SellerStatus

from .exceptions import (
    AlreadyBookedThen,
    BookingNotReserved,
    BookingStarted,
    InvalidSlot,
    RentalNotAvailable,
    SlotUnavailable,
)
from .models import Booking, BookingStatus


def _local(day: date, t) -> datetime:
    return timezone.make_aware(datetime.combine(day, t), timezone.get_current_timezone())


def _ensure_bookable(good: Good) -> RentalSettings:
    position = good.service_position
    if (
        good.kind != GoodKind.RENTAL
        or good.deleted_at is not None
        or not good.is_active
        or position.deleted_at is not None
        or not position.is_active
        or position.seller.status != SellerStatus.ACTIVE
    ):
        raise RentalNotAvailable()
    try:
        return good.rental
    except RentalSettings.DoesNotExist as exc:
        raise RentalNotAvailable(_("This rental has no booking rules yet.")) from exc


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    """Courts have no opening hours: a day is 00:00 to the next 00:00 (company-local)."""
    return _local(day, datetime.min.time()), _local(day + timedelta(days=1), datetime.min.time())


def slots_for_day(good: Good, day: date) -> list[dict]:
    """Every slot of `day` (company-local) with whether it can still be booked."""
    rules = _ensure_bookable(good)
    step = timedelta(minutes=rules.slot_minutes)
    opening, closing = _day_bounds(day)
    booked = list(
        Booking.objects.filter(good=good, start__lt=closing, end__gt=opening).values_list(
            "start", "end"
        )
    )
    now = timezone.now()
    horizon = timezone.localdate() + timedelta(days=rules.max_days_ahead)
    slots, start = [], opening
    while start + step <= closing:
        end = start + step
        taken = any(b_start < end and b_end > start for b_start, b_end in booked)
        if taken:
            state = "BOOKED"
        elif start <= now:
            state = "PAST"
        elif day > horizon:
            state = "NOT_YET_OPEN"
        else:
            state = "FREE"
        slots.append({"start": start, "end": end, "available": state == "FREE", "state": state})
        start = end
    return slots


def day_schedule(good: Good, day: date, with_bookings: bool = False) -> dict:
    """One court's day as periods: consecutive slots with the same state merged, e.g.
    FREE 08:00-10:00, BOOKED 10:00-12:00, FREE 12:00-20:00. Closed days have none."""
    periods = []
    for slot in slots_for_day(good, day):
        if periods and periods[-1]["state"] == slot["state"]:
            periods[-1]["end"] = slot["end"]
        else:
            periods.append({"state": slot["state"], "start": slot["start"], "end": slot["end"]})
    rules = good.rental
    opening, closing = _day_bounds(day)
    day_bookings = (
        Booking.objects.filter(good=good, start__lt=closing, end__gt=opening)
        .select_related("developer", "good__rental", "court_price")
        .order_by("start")
    )
    return {
        "good": good,
        "bookings": [booking_brief(b) for b in day_bookings] if with_bookings else [],
        # Open all day, every day (no opening hours).
        "open": True,
        "opening_time": datetime.min.time(),
        "closing_time": datetime.max.time().replace(microsecond=0),
        "slot_minutes": rules.slot_minutes,
        "periods": periods,
    }


def _validate_slot(
    rules: RentalSettings, start: datetime, slots: int, running_ok: bool = False
) -> datetime:
    """Check the requested range against the rental's rules; return its end. The start may
    be any minute of any day (no opening hours; it may run past midnight); the length is
    whole slots (the price is per slot).
    `running_ok`: the range may have started up to one slot ago ("play now": the start is
    fixed when the desk opens the payment, the PIN comes a little later)."""
    local_start = timezone.localtime(start)
    day = local_start.date()
    step = timedelta(minutes=rules.slot_minutes)
    end = start + step * slots

    if slots < 1:
        raise InvalidSlot(_("Book at least one slot."))
    now = timezone.now()
    if start <= now and not (running_ok and start + step > now):
        raise InvalidSlot(_("This slot has already started."))
    if day > timezone.localdate() + timedelta(days=rules.max_days_ahead):
        raise InvalidSlot(
            _("Bookings open %(days)s days in advance.") % {"days": rules.max_days_ahead},
            details={"max_days_ahead": rules.max_days_ahead},
        )
    if local_start.second or local_start.microsecond:
        raise InvalidSlot(_("Pick a start time in whole minutes."))
    return end


def time_range(*, good: Good, day: date, start_time, end_time) -> tuple[datetime, int]:
    """(start, number of slots) for a company-local date and start/end time. The start is
    any minute; the length is whole slots, e.g. 10:15-12:15 with 60-minute slots = 2 slots."""
    rules = _ensure_bookable(good)
    start, end = _local(day, start_time), _local(day, end_time)
    if end <= start:  # e.g. 23:00-01:00: it ends the next day
        end = _local(day + timedelta(days=1), end_time)
    minutes = (end - start).total_seconds() / 60
    if minutes <= 0 or minutes % rules.slot_minutes:
        raise InvalidSlot(
            _("The end time must be after the start time, in steps of %(minutes)s minutes.")
            % {"minutes": rules.slot_minutes},
            details={"slot_minutes": rules.slot_minutes},
        )
    return start, int(minutes // rules.slot_minutes)


def _ensure_developer_free(developer, start, end, exclude: int | None = None) -> None:
    clash = (
        Booking.objects.filter(developer=developer, start__lt=end, end__gt=start)
        .exclude(pk=exclude)
        .select_related("good")
        .first()
    )
    if clash is not None:
        raise AlreadyBookedThen(
            details={
                "booking": clash.pk,
                "good": clash.good.name,
                "start": clash.start.isoformat(),
                "end": clash.end.isoformat(),
                "date": timezone.localtime(clash.start).date().isoformat(),
                "start_time": timezone.localtime(clash.start).strftime("%H:%M"),
                "end_time": timezone.localtime(clash.end).strftime("%H:%M"),
            }
        )


def _constraint_error(exc: IntegrityError) -> Exception:
    """A concurrent request won: either the court or the developer's time is taken."""
    cause = getattr(exc.__cause__, "diag", None)
    if cause is not None and cause.constraint_name == "booking_one_court_per_developer":
        return AlreadyBookedThen()
    return SlotUnavailable()


def pick_price(good: Good, price_id: int | None = None) -> CourtPrice | None:
    """Which of the court's prices: `price_id`, or its first active one. None only for a
    court without prices (then its own price, counted by `price_per`)."""
    rows = list(CourtPrice.objects.filter(good=good, is_active=True))
    if price_id is not None:
        chosen = next((row for row in rows if row.pk == price_id), None)
        if chosen is None:
            raise InvalidSlot(_("Pick one of this court's prices."))
        return chosen
    return rows[0] if rows else None


def _per(good: Good, court_price: CourtPrice | None) -> str:
    return court_price.per if court_price is not None else good.rental.price_per


def _unit(good: Good, court_price: CourtPrice | None) -> Decimal:
    return court_price.price if court_price is not None else good.price


def people_for(rules: RentalSettings, people: int | None, per: str | None = None) -> int:
    """How many people a booking is for: 1 for a per court price; 1..max_people else."""
    if (per or rules.price_per) != PricePer.PERSON:
        return 1
    people = people or 1
    if people < 1 or people > rules.max_people:
        raise InvalidSlot(
            _("Between 1 and %(max)s people.") % {"max": rules.max_people},
            details={"max_people": rules.max_people},
        )
    return people


def price_of(
    good: Good, slots: int, people: int = 1, court_price: CourtPrice | None = None
) -> Decimal:
    """What a booking costs: the chosen price x slots, times the people for a per person
    price."""
    count = people if _per(good, court_price) == PricePer.PERSON else 1
    return _unit(good, court_price) * slots * count


def play_start(rules: RentalSettings, now: datetime | None = None) -> datetime | None:
    """When "play now" starts: this minute (courts have no opening hours)."""
    return (now or timezone.now()).replace(second=0, microsecond=0)


def check_line(*, good: Good, start: datetime, slots: int, running_ok: bool = False) -> datetime:
    """Early feedback while the desk prepares a booking: the rental is bookable, the range
    fits its rules and is free right now. Confirmation re-checks everything under locks.
    Returns the end of the range."""
    rules = _ensure_bookable(good)
    end = _validate_slot(rules, start, slots, running_ok=running_ok)
    if Booking.objects.filter(good=good, start__lt=end, end__gt=start).exists():
        raise SlotUnavailable()
    return end


# --- Booking for free, paid on arrival ---------------------------------------------------


@transaction.atomic
def reserve(
    *,
    actor,
    good: Good,
    developer: Developer,
    day: date,
    start_time,
    end_time,
    people=1,
    price: int | None = None,
):
    """Book a court for free: the time is held for `developer`, who pays on arrival
    (`start_checkout(booking=...)`). No money moves now."""
    good = Good.all_objects.select_for_update(of=("self",)).select_related("rental").get(pk=good.pk)
    rules = _ensure_bookable(good)
    start, slots = time_range(good=good, day=day, start_time=start_time, end_time=end_time)
    end = _validate_slot(rules, start, slots)
    court_price = pick_price(good, price)
    people = people_for(rules, people, _per(good, court_price))
    if Booking.objects.filter(good=good, start__lt=end, end__gt=start).exists():
        raise SlotUnavailable()
    _ensure_developer_free(developer, start, end)
    try:
        with transaction.atomic():
            booking = Booking.objects.create(
                good=good,
                developer=developer,
                start=start,
                end=end,
                slots=slots,
                people=people,
                court_price=court_price,
                status=BookingStatus.RESERVED,
                reserved_by=actor if getattr(actor, "pk", None) else None,
            )
    except IntegrityError as exc:
        raise _constraint_error(exc) from exc
    record_audit(
        "booking.reserved",
        actor=actor,
        entity=booking,
        new_values={
            "good": good.pk,
            "developer": developer.pk,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "people": people,
            "price": court_price.pk if court_price else None,
        },
    )
    return booking


@transaction.atomic
def cancel_reservation(*, actor, booking: Booking) -> None:
    """An unpaid booking is dropped (no money moved). Paid ones are refunded instead
    (`purchases.services.refund_purchase`)."""
    booking = Booking.objects.select_for_update().get(pk=booking.pk)
    if booking.status != BookingStatus.RESERVED:
        raise BookingNotReserved()
    record_audit(
        "booking.cancelled",
        actor=actor,
        entity=booking,
        old_values={
            "good": booking.good_id,
            "developer": booking.developer_id,
            "start": booking.start.isoformat(),
            "end": booking.end.isoformat(),
        },
    )
    booking.delete()


@transaction.atomic
def pay_reservation(*, actor, purchase: Purchase, developer: Developer, item) -> Booking:
    """Called by `confirm_purchase` (in its transaction): the reserved booking this purchase
    pays becomes PAID."""
    booking = (
        Booking.objects.select_for_update(of=("self",))
        .select_related("good__service_position", "good__rental")
        .get(pk=purchase.booking_to_pay_id)
    )
    if booking.status != BookingStatus.RESERVED:
        raise BookingNotReserved()
    if (booking.good_id, booking.start, booking.slots) != (item.good_id, item.start, item.quantity):
        raise SlotUnavailable(_("The booking changed meanwhile; start the payment again."))
    booking.status = BookingStatus.PAID
    booking.purchase = purchase
    booking.people = item.people
    booking.court_price_id = item.court_price_id
    booking.save(update_fields=["status", "purchase", "people", "court_price", "updated_at"])
    record_audit(
        "booking.paid",
        actor=actor,
        entity=booking,
        new_values={
            "purchase": purchase.pk,
            "developer": developer.pk,
            "total": str(purchase.total),
        },
    )
    return booking


def book_lines(
    *, actor, purchase: Purchase, developer: Developer, items, goods: dict
) -> list[Booking]:
    """Create the bookings of a purchase being confirmed (the card holder pays).

    Called by `confirm_purchase` inside its transaction, with the developer's account and
    the rental goods already locked: bookings of one court, and of one developer, are
    processed one at a time. The exclusion constraints are the final guard.
    """
    bookings = []
    for item in items:
        good = goods[item.good_id]
        rules = _ensure_bookable(good)
        end = _validate_slot(rules, item.start, item.quantity, running_ok=True)
        if Booking.objects.filter(good=good, start__lt=end, end__gt=item.start).exists():
            raise SlotUnavailable(details={"good": good.pk, "start": item.start.isoformat()})
        _ensure_developer_free(developer, item.start, end)
        try:
            with transaction.atomic():
                booking = Booking.objects.create(
                    good=good,
                    developer=developer,
                    purchase=purchase,
                    start=item.start,
                    end=end,
                    slots=item.quantity,
                    people=item.people,
                    court_price_id=item.court_price_id,
                    status=BookingStatus.PAID,
                )
        except IntegrityError as exc:
            raise _constraint_error(exc) from exc
        record_audit(
            "booking.created",
            actor=actor,
            entity=booking,
            new_values={
                "good": good.pk,
                "developer": developer.pk,
                "start": booking.start.isoformat(),
                "end": booking.end.isoformat(),
                "slots": booking.slots,
                "purchase": purchase.pk,
            },
        )
        bookings.append(booking)
    return bookings


# --- Checkout: a court booking is paid like a till sale, but on its own -------------------


@transaction.atomic
def start_checkout(
    *,
    actor,
    good: Good | None = None,
    day: date | None = None,
    start_time=None,
    end_time=None,
    reader=None,
    client_ip=None,
    people: int = 1,
    now_slots: int | None = None,
    booking: Booking | None = None,
    event=None,
    price: int | None = None,
) -> Purchase:
    """A draft BOOKING purchase for one court and time; the developer taps their card on
    the desk's reader and enters the PIN, then `confirm_purchase` books it for the card
    holder. The time is not held until then. Three kinds:

    - a day and time (`day`, `start_time`, `end_time`);
    - "play now" (`now_slots`): from this minute, for that many slots;
    - a reserved booking paid on arrival (`booking`): its court, time and people.

    `event`: a card tap just made on the desk's reader (e.g. at arrival) counts as the
    card, so the developer only types the PIN."""
    from apps.purchases import services as purchases
    from apps.purchases.models import PurchaseItem, PurchaseKind

    if booking is not None:
        booking = Booking.objects.select_related(
            "good__service_position__seller", "good__rental"
        ).get(pk=booking.pk)
        if booking.status != BookingStatus.RESERVED:
            raise BookingNotReserved()
        if booking.end <= timezone.now():
            raise BookingStarted(_("This booking is over."))
        good, start, slots, people = booking.good, booking.start, booking.slots, booking.people
        court_price = booking.court_price
        _ensure_bookable(good)
    else:
        good = Good.all_objects.select_related("service_position__seller", "rental").get(pk=good.pk)
        rules = _ensure_bookable(good)
        court_price = pick_price(good, price)
        people = people_for(rules, people, _per(good, court_price))
        if now_slots is not None:
            start = play_start(rules)
            if start is None:
                raise InvalidSlot(_("The court is closed now."))
            slots = now_slots
            check_line(good=good, start=start, slots=slots, running_ok=True)
        else:
            start, slots = time_range(good=good, day=day, start_time=start_time, end_time=end_time)
            check_line(good=good, start=start, slots=slots)
    purchase = purchases.create_purchase(
        actor=actor,
        service_position=good.service_position,
        reader=reader,
        client_ip=client_ip,
        kind=PurchaseKind.BOOKING,
    )
    PurchaseItem.objects.create(
        purchase=purchase,
        good=good,
        quantity=slots,
        start=start,
        people=people,
        court_price=court_price,
    )
    if booking is not None:
        purchase.booking_to_pay = booking
        purchase.save(update_fields=["booking_to_pay", "updated_at"])
    if event is not None:
        purchases.present_tap(purchase, event)
    elif purchase.reader_id is not None:
        # Wait for the card from now on: a tap the moment the screen asks for it counts.
        purchases.wait_for_card(purchase=purchase)
    return purchase


# --- The desk: courts now, arrivals ------------------------------------------------------


def booking_brief(booking: Booking) -> dict:
    """A booking as the desk shows it on the board and at arrival."""
    price = price_of(booking.good, booking.slots, booking.people, booking.court_price)
    developer = booking.developer
    return {
        "id": booking.pk,
        "good": booking.good_id,
        "good_name": booking.good.name,
        "start": booking.start,
        "end": booking.end,
        "start_time": f"{timezone.localtime(booking.start):%H:%M}",
        "end_time": f"{timezone.localtime(booking.end):%H:%M}",
        "date": timezone.localtime(booking.start).date().isoformat(),
        "status": booking.status,
        "place": getattr(getattr(booking.good, "rental", None), "place", "OUTDOOR"),
        "people": booking.people,
        "price": f"{price:.2f}",
        "price_name": booking.court_price.name if booking.court_price_id else "",
        "purchase": booking.purchase_id,
        "developer": {
            "id": developer.pk,
            "full_name": developer.full_name,
            "employee_number": developer.employee_number,
        },
    }


def now_board(goods) -> list[dict]:
    """Each court now: CLOSED, FREE (until the next booking or closing) or IN_USE (by
    whom, until when, paid or not), and its next booking today."""
    now = timezone.now()
    today = timezone.localdate()
    goods = list(goods.select_related("rental").prefetch_related("court_prices"))
    bookings = (
        Booking.objects.filter(
            good__in=goods,
            end__gt=now,
            start__lt=_local(today + timedelta(days=1), datetime.min.time()),
        )
        .select_related("developer", "good__rental", "court_price")
        .order_by("start")
    )
    by_court: dict[int, list[Booking]] = {}
    for booking in bookings:
        by_court.setdefault(booking.good_id, []).append(booking)
    board = []
    for good in goods:
        rules = good.rental
        running = play_start(rules, now)
        mine = by_court.get(good.pk, [])
        current = next((b for b in mine if b.start <= now < b.end), None)
        upcoming = next((b for b in mine if b.start > now), None)
        if current is not None:
            state, until = "IN_USE", current.end
        else:
            state, until = "FREE", upcoming.start if upcoming else None
        # How many slots "play now" can still take before the next booking (no closing time;
        # the desk offers up to 4).
        step = timedelta(minutes=rules.slot_minutes)
        fits = (4 if until is None else int((until - running) / step)) if state == "FREE" else 0
        board.append(
            {
                "good": good.pk,
                "name": good.name,
                "price": f"{good.price:.2f}",
                "price_per": rules.price_per,
                "prices": [
                    {"id": p.pk, "name": p.name, "price": f"{p.price:.2f}", "per": p.per}
                    for p in good.court_prices.all()
                    if p.is_active
                ],
                "max_people": rules.max_people,
                "slot_minutes": rules.slot_minutes,
                "state": state,
                "until": until,
                "until_time": f"{timezone.localtime(until):%H:%M}" if until else None,
                "play_slots": fits,
                "now": booking_brief(current) if current else None,
                "next": booking_brief(upcoming) if upcoming else None,
            }
        )
    return board


def arrival(reader, after: int | None) -> dict:
    """The desk's arrival tap: the newest accepted tap on `reader` after `after` (an event
    id), its developer, their booking now (or starting within
    PLAYGROUND_CHECK_IN_EARLY_MINUTES, to pay it on arrival) and their later bookings
    today. `cursor` is the newest tap id (for the next poll)."""
    from apps.rfid.models import RFIDEvent

    taps = RFIDEvent.objects.filter(device=reader).order_by("-pk")
    body = {"cursor": taps.values_list("pk", flat=True).first(), "tap": None}
    if after is None:
        return body
    tap = taps.filter(pk__gt=after).select_related("developer").first()
    if tap is None:
        return body
    developer = tap.developer
    entry = {
        "event": tap.pk,
        "uid": tap.uid,
        "result": tap.result,
        "developer": None,
        "now": None,
        "later": [],
    }
    if developer is not None and tap.result == "ACCEPTED":
        now = timezone.now()
        early = timedelta(minutes=settings.PLAYGROUND_CHECK_IN_EARLY_MINUTES)
        mine = list(
            Booking.objects.filter(
                developer=developer,
                end__gt=now,
                start__lt=_local(timezone.localdate() + timedelta(days=1), datetime.min.time()),
            )
            .select_related("good__rental", "developer", "court_price")
            .order_by("start")
        )
        current = next((b for b in mine if b.start <= now + early), None)
        entry["developer"] = {
            "id": developer.pk,
            "full_name": developer.full_name,
            "employee_number": developer.employee_number,
            "department": developer.department,
            "photo": developer.photo.url if developer.photo else None,
        }
        entry["now"] = booking_brief(current) if current else None
        entry["later"] = [booking_brief(b) for b in mine if b is not current]
    body["tap"] = entry
    return body


# --- Changing a paid booking -------------------------------------------------------------


def change_booking(
    *,
    actor,
    booking: Booking,
    day: date,
    start_time,
    end_time,
    good: Good | None = None,
    pin: str | None = None,
) -> Booking:
    """Move a paid booking to another time and/or another court of the same seller.

    Allowed until the booking starts (later too if its court allows `refund_after_start`,
    e.g. to rebook a no-show). The same rules as a new booking apply,
    ignoring the booking itself. The new time may cost more or less than what was paid: a
    cheaper one refunds the difference to the wallet, a dearer one charges it and needs the
    developer's card tap on the purchase's reader and their `pin` (as at checkout). The
    purchase's line and total follow the booking.
    Locks: purchase, booking, account, then the courts by id (as at checkout).
    """
    from apps.finance import services as finance
    from apps.finance.models import TransactionKind
    from apps.purchases import services as purchases

    if pin:
        # Own transaction (as at checkout), so a wrong PIN is counted even if we fail later.
        finance.verify_pin(account=finance.open_account(booking.developer), pin=pin)

    with transaction.atomic():
        # A reserved (unpaid) booking has no purchase: it just moves, no money.
        purchase = (
            Purchase.objects.select_for_update().get(pk=booking.purchase_id)
            if booking.purchase_id
            else None
        )
        booking = (
            Booking.objects.select_for_update(of=("self",))
            .select_related("good__rental", "developer")
            .get(pk=booking.pk)
        )
        rules_now = getattr(booking.good, "rental", None)
        if booking.start <= timezone.now() and not (rules_now and rules_now.refund_after_start):
            raise BookingStarted()
        target = good or booking.good
        account = (
            DeveloperAccount.objects.select_for_update()
            .filter(developer_id=booking.developer_id)
            .first()
        )
        courts = {
            g.pk: g
            for g in Good.all_objects.select_for_update(of=("self",))
            .select_related("service_position__seller", "rental")
            .filter(pk__in={booking.good_id, target.pk})
            .order_by("pk")
        }
        target = courts[target.pk]
        if target.service_position.seller_id != booking.good.service_position.seller_id:
            raise RentalNotAvailable(_("A booking can only move to a court of the same seller."))
        rules = _ensure_bookable(target)
        start, slots = time_range(good=target, day=day, start_time=start_time, end_time=end_time)
        end = _validate_slot(rules, start, slots)

        # The same price on the same court; on another court its price of the same name
        # (else its first one).
        court_price = booking.court_price
        if target.pk != booking.good_id:
            choices = list(CourtPrice.objects.filter(good=target, is_active=True))
            court_price = next(
                (p for p in choices if court_price and p.name == court_price.name),
                choices[0] if choices else None,
            )
        people = people_for(rules, booking.people, _per(target, court_price))
        # Slots given free by the preferential service stay free on the new time.
        line = purchase.items.first() if purchase is not None else None
        free = line.free_quantity if line is not None else 0
        price = finance.money(price_of(target, max(slots - free, 0), people, court_price))
        paid = purchase.total if purchase is not None else price
        difference = price - paid
        if difference > 0:
            purchases.ensure_payer_present(purchase, pin)

        if (
            Booking.objects.filter(good=target, start__lt=end, end__gt=start)
            .exclude(pk=booking.pk)
            .exists()
        ):
            raise SlotUnavailable()
        _ensure_developer_free(booking.developer, start, end, exclude=booking.pk)

        old = {
            "good": booking.good_id,
            "start": booking.start.isoformat(),
            "end": booking.end.isoformat(),
            "slots": booking.slots,
            "total": str(paid),
        }
        booking.good, booking.start, booking.end, booking.slots = target, start, end, slots
        booking.court_price, booking.people = court_price, people
        booking.change_count += 1
        try:
            with transaction.atomic():
                booking.save()
        except IntegrityError as exc:
            raise _constraint_error(exc) from exc

        txn = None
        if purchase is not None and difference:
            txn = finance.post_transaction(
                account=account,
                kind=TransactionKind.PURCHASE if difference > 0 else TransactionKind.REFUND,
                amount=-difference,
                actor=actor,
                description=f"Booking {booking.pk} changed",
                reference=f"purchase:{purchase.pk}",
                allow_minus=True,
            )
            if difference > 0:
                purchases.clear_presented(purchase)
        if purchase is not None:
            purchase.items.update(
                good=target,
                start=start,
                quantity=slots,
                people=people,
                court_price=court_price,
                unit_price=_unit(target, court_price),
                line_total=price,
            )
            if price != paid:
                purchase.total = price
                purchase.save(update_fields=["total", "updated_at"])

        record_audit(
            "booking.changed",
            actor=actor,
            entity=booking,
            old_values=old,
            new_values={
                "good": target.pk,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "slots": slots,
                "total": str(price),
                "difference": str(difference),
                "balance_after": str(
                    txn.balance_after if txn else (account.balance if account else 0)
                ),
            },
        )
    return booking


def desk_people(*, search: str = "", pk: int | None = None, limit: int = 8) -> list[dict]:
    """Who a desk booking is for: active developers by name or employee number (or one by
    id), with what the desk shows: photo, team, building, card and wallet balance (they
    pay on arrival). The playground is shared, so every building."""
    from apps.developers.models import DeveloperStatus
    from apps.rfid.models import RFIDCardAssignment

    people = Developer.objects.select_related("building", "account")
    if pk is not None:
        people = people.filter(pk=pk)
    else:
        search = search.strip()
        if not search:
            return []
        people = people.filter(status=DeveloperStatus.ACTIVE).filter(
            Q(full_name__icontains=search) | Q(employee_number__icontains=search)
        )
    people = list(people.order_by("full_name", "id")[:limit])
    cards = dict(
        RFIDCardAssignment.objects.filter(
            developer__in=people, unassigned_at__isnull=True
        ).values_list("developer_id", "card__label")
    )
    rows = []
    for person in people:
        account = getattr(person, "account", None)
        rows.append(
            {
                "id": person.pk,
                "full_name": person.full_name,
                "employee_number": person.employee_number,
                "department": person.department,
                "position_title": person.position_title,
                "building_name": person.building.name if person.building_id else None,
                "card_label": cards.get(person.pk) or None,
                "status": person.status,
                "photo": person.photo.url if person.photo else None,
                "balance": f"{account.balance:.2f}" if account is not None else None,
            }
        )
    return rows
