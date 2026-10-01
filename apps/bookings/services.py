from datetime import date, datetime, timedelta

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.audit.services import record_audit
from apps.developers.models import Developer
from apps.goods.models import Good, GoodKind, RentalSettings
from apps.purchases.models import Purchase
from apps.sellers.models import SellerStatus

from .exceptions import (
    AlreadyBookedThen,
    DailyLimitReached,
    InvalidSlot,
    RentalNotAvailable,
    SlotUnavailable,
)
from .models import Booking


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
        slots.append(
            {"start": start, "end": end, "available": not taken and start > now and day <= horizon}
        )
        start = end
    return slots


def _validate_slot(rules: RentalSettings, start: datetime, slots: int) -> datetime:
    """Check the requested range against the rental's rules; return its end."""
    local_start = timezone.localtime(start)
    day = local_start.date()
    step = timedelta(minutes=rules.slot_minutes)
    end = start + step * slots
    opening, closing = _local(day, rules.opening_time), _local(day, rules.closing_time)

    if not 1 <= slots <= rules.max_slots_per_booking:
        raise InvalidSlot(
            _("Book between 1 and %(max)s slots.") % {"max": rules.max_slots_per_booking},
            details={"max_slots_per_booking": rules.max_slots_per_booking},
        )
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


def _ensure_daily_limit(rules: RentalSettings, good: Good, developer, start, slots) -> None:
    """One developer may book at most `max_slots_per_day` slots of a rental per local day,
    across all their bookings. Safe under concurrency: the caller holds the developer's
    account lock, so one developer's bookings are processed one at a time."""
    day = timezone.localtime(start).date()
    day_start = _local(day, datetime.min.time())
    already = (
        Booking.objects.filter(
            good=good,
            developer=developer,
            start__gte=day_start,
            start__lt=day_start + timedelta(days=1),
        ).aggregate(n=Sum("slots"))["n"]
        or 0
    )
    if already + slots > rules.max_slots_per_day:
        raise DailyLimitReached(
            details={
                "max_slots_per_day": rules.max_slots_per_day,
                "already_booked": already,
                "remaining": max(rules.max_slots_per_day - already, 0),
            }
        )


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
        _ensure_daily_limit(rules, good, developer, item.start, item.quantity)
        clash = (
            Booking.objects.filter(developer=developer, start__lt=end, end__gt=item.start)
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
            # A concurrent confirmation won: either the court or the developer's time is taken.
            cause = getattr(exc.__cause__, "diag", None)
            if cause is not None and cause.constraint_name == "booking_one_court_per_developer":
                raise AlreadyBookedThen() from exc
            raise SlotUnavailable() from exc
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
