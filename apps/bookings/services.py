from datetime import date, datetime, timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.audit.services import record_audit
from apps.developers.models import Developer
from apps.finance.models import DeveloperAccount
from apps.goods.models import Good, GoodKind, RentalSettings
from apps.purchases.models import Purchase
from apps.sellers.models import SellerStatus

from .exceptions import (
    AlreadyBookedThen,
    BookingCheckedIn,
    BookingPriceDifferent,
    BookingStarted,
    InvalidSlot,
    RentalNotAvailable,
    SlotUnavailable,
)
from .models import Booking, CheckResult, PlaygroundCheck


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


def slots_for_day(good: Good, day: date) -> list[dict]:
    """Every slot of `day` (company-local) with whether it can still be booked."""
    rules = _ensure_bookable(good)
    if day.weekday() not in rules.weekdays:
        return []
    step = timedelta(minutes=rules.slot_minutes)
    opening, closing = _local(day, rules.opening_time), _local(day, rules.closing_time)
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


def day_schedule(good: Good, day: date) -> dict:
    """One court's day as periods: consecutive slots with the same state merged, e.g.
    FREE 08:00-10:00, BOOKED 10:00-12:00, FREE 12:00-20:00. Closed days have none."""
    periods = []
    for slot in slots_for_day(good, day):
        if periods and periods[-1]["state"] == slot["state"]:
            periods[-1]["end"] = slot["end"]
        else:
            periods.append({"state": slot["state"], "start": slot["start"], "end": slot["end"]})
    rules = good.rental
    return {
        "good": good,
        "open": day.weekday() in rules.weekdays,
        "opening_time": rules.opening_time,
        "closing_time": rules.closing_time,
        "slot_minutes": rules.slot_minutes,
        "periods": periods,
    }


def _validate_slot(rules: RentalSettings, start: datetime, slots: int) -> datetime:
    """Check the requested range against the rental's rules; return its end."""
    local_start = timezone.localtime(start)
    day = local_start.date()
    step = timedelta(minutes=rules.slot_minutes)
    end = start + step * slots
    opening, closing = _local(day, rules.opening_time), _local(day, rules.closing_time)

    if slots < 1:
        raise InvalidSlot(_("Book at least one slot."))
    if start <= timezone.now():
        raise InvalidSlot(_("This slot has already started."))
    if day > timezone.localdate() + timedelta(days=rules.max_days_ahead):
        raise InvalidSlot(
            _("Bookings open %(days)s days in advance.") % {"days": rules.max_days_ahead},
            details={"max_days_ahead": rules.max_days_ahead},
        )
    if day.weekday() not in rules.weekdays:
        raise InvalidSlot(_("The rental is closed on this day."))
    offset = (local_start - opening).total_seconds()
    if offset < 0 or offset % step.total_seconds() or end > closing:
        raise InvalidSlot(
            _("Pick a start time on the slot grid within opening hours."),
            details={
                "opening_time": rules.opening_time.isoformat(),
                "closing_time": rules.closing_time.isoformat(),
                "slot_minutes": rules.slot_minutes,
            },
        )
    return end


def time_range(*, good: Good, day: date, start_time, end_time) -> tuple[datetime, int]:
    """(start, number of slots) for a company-local date and start/end time. The times
    must be on the rental's slot grid, e.g. 10:00-12:00 with 60-minute slots = 2 slots."""
    rules = _ensure_bookable(good)
    start, end = _local(day, start_time), _local(day, end_time)
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
            }
        )


def _constraint_error(exc: IntegrityError) -> Exception:
    """A concurrent request won: either the court or the developer's time is taken."""
    cause = getattr(exc.__cause__, "diag", None)
    if cause is not None and cause.constraint_name == "booking_one_court_per_developer":
        return AlreadyBookedThen()
    return SlotUnavailable()


def check_line(*, good: Good, start: datetime, slots: int) -> datetime:
    """Early feedback while the desk prepares a booking: the rental is bookable, the range
    fits its rules and is free right now. Confirmation re-checks everything under locks.
    Returns the end of the range."""
    rules = _ensure_bookable(good)
    end = _validate_slot(rules, start, slots)
    if Booking.objects.filter(good=good, start__lt=end, end__gt=start).exists():
        raise SlotUnavailable()
    return end


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
        end = _validate_slot(rules, item.start, item.quantity)
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
    *, actor, good: Good, day: date, start_time, end_time, reader=None, client_ip=None
) -> Purchase:
    """A draft BOOKING purchase holding exactly this court and time. The developer taps
    their card on the desk's reader and enters the PIN; `confirm_purchase` then books it
    for the card holder. The time is not held until then."""
    from apps.purchases import services as purchases
    from apps.purchases.models import PurchaseItem, PurchaseKind

    good = Good.all_objects.select_related("service_position__seller").get(pk=good.pk)
    start, slots = time_range(good=good, day=day, start_time=start_time, end_time=end_time)
    check_line(good=good, start=start, slots=slots)
    purchase = purchases.create_purchase(
        actor=actor,
        service_position=good.service_position,
        reader=reader,
        client_ip=client_ip,
        kind=PurchaseKind.BOOKING,
    )
    PurchaseItem.objects.create(purchase=purchase, good=good, quantity=slots, start=start)
    return purchase


# --- Changing a paid booking -------------------------------------------------------------


@transaction.atomic
def change_booking(
    *, actor, booking: Booking, day: date, start_time, end_time, good: Good | None = None
) -> Booking:
    """Move a paid booking to another time and/or another court of the same seller.

    Allowed until the booking starts. For now the new time must cost the same as what was
    paid (no money moves); the same rules as a new booking apply, ignoring the booking
    itself. Locks: account, then the courts by id (as at checkout).
    """
    booking = Booking.objects.select_for_update().select_related("purchase").get(pk=booking.pk)
    if booking.start <= timezone.now():
        raise BookingStarted()
    if booking.checked_in_at is not None:
        raise BookingCheckedIn()
    target = good or booking.good
    DeveloperAccount.objects.select_for_update().filter(developer=booking.developer_id).first()
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

    paid = booking.purchase.total
    price = target.price * slots
    if price != paid:
        raise BookingPriceDifferent(details={"paid": str(paid), "new_price": str(price)})

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
    }
    booking.good, booking.start, booking.end, booking.slots = target, start, end, slots
    booking.change_count += 1
    try:
        with transaction.atomic():
            booking.save()
    except IntegrityError as exc:
        raise _constraint_error(exc) from exc
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
        },
    )
    return booking


# --- Checking in at the playground -------------------------------------------------------


def booking_to_check(developer, now=None) -> Booking | None:
    """The developer's booking a check-in tap counts for: running now, or starting within
    PLAYGROUND_CHECK_IN_EARLY_MINUTES. The earliest first."""
    from django.conf import settings

    now = now or timezone.now()
    early = timedelta(minutes=settings.PLAYGROUND_CHECK_IN_EARLY_MINUTES)
    return (
        Booking.objects.select_for_update()
        .filter(developer=developer, start__lte=now + early, end__gt=now)
        .order_by("start", "id")
        .first()
    )


@transaction.atomic
def check_in(event) -> PlaygroundCheck:
    """A card tap on a playground check reader: check the card holder in to their booking
    (see `booking_to_check`) and keep what the tap found for the checker's page."""
    from apps.rfid.models import ScanResult

    reader = event.device
    check = PlaygroundCheck(
        event=event, reader=reader, checker_id=reader.checker_id, developer=event.developer
    )
    if event.result not in (ScanResult.ACCEPTED, ScanResult.DUPLICATE) or event.developer is None:
        check.result, check.scan_result = CheckResult.CARD_REFUSED, event.result
    else:
        booking = booking_to_check(event.developer)
        check.booking = booking
        if booking is None:
            check.result = CheckResult.NO_BOOKING
        elif booking.checked_in_at is not None:
            check.result = CheckResult.ALREADY_CHECKED_IN
        else:
            check.result = CheckResult.CHECKED_IN
            booking.checked_in_at = timezone.now()
            booking.checked_in_by_id = reader.checker_id
            booking.save(update_fields=["checked_in_at", "checked_in_by", "updated_at"])
            record_audit(
                "booking.checked_in",
                actor=reader.checker,
                entity=booking,
                new_values={"reader": reader.code},
            )
    check.save()
    return check
