from datetime import date, datetime, timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.services import record_audit
from apps.developers.models import Developer
from apps.finance import services as finance
from apps.finance.exceptions import IdempotencyKeyReused
from apps.finance.models import DeveloperAccount, TransactionKind
from apps.goods.models import Good, GoodKind, RentalSettings
from apps.purchases.exceptions import DeveloperNotActive
from apps.purchases.models import Purchase, PurchaseItem, PurchaseStatus
from apps.purchases.services import INACTIVE_DEVELOPER
from apps.seller_finance.services import credit_sale
from apps.sellers.models import SellerStatus

from .exceptions import InvalidSlot, RentalNotAvailable, SlotUnavailable
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
    return good.rental


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
            f"Book between 1 and {rules.max_slots_per_booking} slots.",
            details={"max_slots_per_booking": rules.max_slots_per_booking},
        )
    if start <= timezone.now():
        raise InvalidSlot("This slot has already started.")
    if day > timezone.localdate() + timedelta(days=rules.max_days_ahead):
        raise InvalidSlot(
            f"Bookings open {rules.max_days_ahead} days in advance.",
            details={"max_days_ahead": rules.max_days_ahead},
        )
    if day.weekday() not in rules.weekdays:
        raise InvalidSlot("The rental is closed on this day.")
    offset = (local_start - opening).total_seconds()
    if offset < 0 or offset % step.total_seconds() or end > closing:
        raise InvalidSlot(
            "Pick a start time on the slot grid within opening hours.",
            details={
                "opening_time": rules.opening_time.isoformat(),
                "closing_time": rules.closing_time.isoformat(),
                "slot_minutes": rules.slot_minutes,
            },
        )
    return end


def book(
    *,
    actor,
    developer: Developer,
    good: Good,
    start: datetime,
    slots: int,
    pin: str,
    idempotency_key: str,
) -> tuple[Booking, bool]:
    """Book and pay for a rental slot range. Returns (booking, created).

    1. Replay: the same Idempotency-Key returns the original booking.
    2. Verify the developer's PIN (own transaction, failures are counted).
    3. One atomic transaction, locking account then good (same order as checkout):
       re-check the rental and the slot, reject overlaps, charge the developer, credit
       the seller, record a confirmed purchase and the booking. The exclusion
       constraint is the final guard against double-booking.
    """
    previous = Purchase.objects.filter(confirm_idempotency_key=idempotency_key).first()
    if previous is not None:
        booking = Booking.objects.filter(purchase=previous).first()
        if booking is None or (
            booking.developer_id,
            booking.good_id,
            booking.start,
            booking.slots,
        ) != (developer.pk, good.pk, start, slots):
            raise IdempotencyKeyReused()
        return booking, False

    if developer.deleted_at or developer.status in INACTIVE_DEVELOPER:
        raise DeveloperNotActive()
    account = finance.open_account(developer)
    finance.verify_pin(account=account, pin=pin)

    try:
        with transaction.atomic():
            account = DeveloperAccount.objects.select_for_update().get(pk=account.pk)
            good = (
                # Lock only the good's row: Postgres can't lock the nullable side of the
                # LEFT JOIN that select_related("rental") produces.
                Good.all_objects.select_for_update(of=("self",))
                .select_related("service_position__seller", "rental")
                .get(pk=good.pk)
            )
            rules = _ensure_bookable(good)
            end = _validate_slot(rules, start, slots)
            if Booking.objects.filter(good=good, start__lt=end, end__gt=start).exists():
                raise SlotUnavailable()

            total = finance.money(good.price * slots)
            if total <= 0:
                raise RentalNotAvailable("Rentals must have a positive price.")
            position = good.service_position
            purchase = Purchase.objects.create(
                seller=position.seller, service_position=position, created_by=actor
            )
            reference = f"purchase:{purchase.pk}"
            PurchaseItem.objects.create(
                purchase=purchase,
                good=good,
                quantity=slots,
                unit_price=good.price,
                line_total=total,
            )
            txn = finance.post_transaction(
                account=account,
                kind=TransactionKind.PURCHASE,
                amount=-total,
                actor=actor,
                description=f"Booking: {good.name}",
                reference=reference,
            )
            credit_sale(seller=position.seller, amount=total, reference=reference, actor=actor)

            purchase.status = PurchaseStatus.CONFIRMED
            purchase.developer = developer
            purchase.total = total
            purchase.account_transaction = txn
            purchase.confirm_idempotency_key = idempotency_key
            purchase.confirmed_by = actor
            purchase.confirmed_at = timezone.now()
            purchase.save()

            try:
                with transaction.atomic():
                    booking = Booking.objects.create(
                        good=good,
                        developer=developer,
                        purchase=purchase,
                        start=start,
                        end=end,
                        slots=slots,
                    )
            except IntegrityError as exc:
                raise SlotUnavailable() from exc

            record_audit(
                "booking.created",
                actor=actor,
                entity=booking,
                new_values={
                    "good": good.pk,
                    "developer": developer.pk,
                    "start": start,
                    "end": end,
                    "total": total,
                    "purchase": purchase.pk,
                },
            )
    except IntegrityError:
        # A concurrent request with the same Idempotency-Key won.
        previous = Purchase.objects.filter(confirm_idempotency_key=idempotency_key).first()
        if previous is None:
            raise
        return Booking.objects.get(purchase=previous), False
    return booking, True
