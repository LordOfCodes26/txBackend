from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError

from apps.audit.services import record_audit
from apps.developers.models import DeveloperStatus
from apps.finance import services as finance
from apps.finance.exceptions import IdempotencyKeyReused
from apps.finance.models import DeveloperAccount, TransactionKind
from apps.goods import services as goods
from apps.goods.exceptions import InsufficientStock
from apps.goods.models import Good, GoodKind, MovementKind
from apps.realtime.notify import notify_purchase
from apps.rfid.models import CardStatus, RFIDCard, RFIDCardAssignment
from apps.sellers.models import SellerStatus, ServicePosition

from .exceptions import (
    CardNotPresented,
    CardNotUsable,
    DeveloperNotActive,
    GoodNotAvailable,
    ManualCardEntryDisabled,
    NoTillReader,
    PaidBookingNotEditable,
    PinRequired,
    PurchaseEmpty,
    PurchaseNotDraft,
    PurchaseNotPaid,
    SelfPurchaseForbidden,
    SellerNotActive,
)
from .models import Purchase, PurchaseItem, PurchaseKind, PurchaseStatus

INACTIVE_DEVELOPER = {DeveloperStatus.SUSPENDED, DeveloperStatus.TERMINATED}
MAX_ITEM_QUANTITY = 999  # matches PurchaseItem.quantity's validator


def _lock_draft(purchase: Purchase) -> Purchase:
    purchase = Purchase.objects.select_for_update().get(pk=purchase.pk)
    if purchase.status != PurchaseStatus.DRAFT:
        raise PurchaseNotDraft()
    return purchase


# A draft takes a card tap to pay; a paid purchase to pay a dearer change.
TAKES_CARD = (PurchaseStatus.DRAFT, PurchaseStatus.CONFIRMED)


def _lock_takes_card(purchase: Purchase) -> Purchase:
    purchase = Purchase.objects.select_for_update().get(pk=purchase.pk)
    if purchase.status not in TAKES_CARD:
        raise PurchaseNotDraft()
    return purchase


def _ensure_sellable(good: Good, purchase: Purchase, *, booking: bool = False) -> None:
    """`booking`: the line is a booking (has a start time); only rentals are booked."""
    if (good.kind == GoodKind.RENTAL) != booking:
        raise GoodNotAvailable(
            _("Courts are booked separately, not added to a till purchase.")
            if good.kind == GoodKind.RENTAL
            else _("Only courts can be booked."),
            details={"good": good.pk},
        )
    if (
        good.deleted_at is not None
        or not good.is_active
        or good.service_position_id != purchase.service_position_id
    ):
        raise GoodNotAvailable(details={"good": good.pk})


def _ensure_seller_active(position: ServicePosition) -> None:
    if (
        position.deleted_at is not None
        or not position.is_active
        or position.seller.status != SellerStatus.ACTIVE
    ):
        raise SellerNotActive()


# --- Draft bucket -------------------------------------------------------------------


def seller_readers(seller_id: int):
    """The till readers assigned to this seller (store), active ones only."""
    from apps.rfid.models import DevicePurpose, RFIDDevice

    return RFIDDevice.objects.filter(
        purpose=DevicePurpose.TILL, is_active=True, seller_id=seller_id
    ).order_by("code")


def usable_readers(seller_id: int, kind: str = PurchaseKind.SALE):
    """The readers a purchase may take its card tap from: the seller's till readers, and
    for a court booking also every card assign (master) reader."""
    from django.db.models import Q

    from apps.rfid.models import DevicePurpose, RFIDDevice

    allowed = Q(purpose=DevicePurpose.TILL, seller_id=seller_id)
    if kind == PurchaseKind.BOOKING:
        allowed |= Q(purpose=DevicePurpose.ENROLL)
    return RFIDDevice.objects.filter(allowed, is_active=True).order_by("purpose", "code")


def _check_reader(seller_id: int, reader, kind: str = PurchaseKind.SALE) -> None:
    if reader is None or usable_readers(seller_id, kind).filter(pk=reader.pk).exists():
        return
    if reader.purpose != "TILL":
        raise ValidationError({"reader": [_("Card assign readers only take court bookings.")]})
    raise ValidationError({"reader": [_("This till reader belongs to another seller.")]})


@transaction.atomic
def create_purchase(
    *,
    actor,
    service_position: ServicePosition,
    reader=None,
    client_ip: str | None = None,
    kind: str = PurchaseKind.SALE,
) -> Purchase:
    """`reader` must be one of the seller's till readers. Left out, the seller's only reader
    is used; a seller with several chooses one (`set_reader`) before scanning."""
    _ensure_seller_active(service_position)
    seller_id = service_position.seller_id
    _check_reader(seller_id, reader, kind)
    if reader is None:
        own = list(seller_readers(seller_id)[:2])
        if len(own) == 1:
            reader = own[0]
    return Purchase.objects.create(
        seller=service_position.seller,
        service_position=service_position,
        created_by=actor,
        reader=reader,
        client_ip=client_ip,
        kind=kind,
    )


@transaction.atomic
def set_reader(*, purchase: Purchase, reader) -> Purchase:
    """Use another of the seller's till readers for this draft. A card tapped on the
    previous reader no longer counts, and the purchase stops waiting for a card."""
    purchase = _lock_takes_card(purchase)
    _check_reader(purchase.seller_id, reader, purchase.kind)
    if purchase.reader_id != getattr(reader, "pk", None):
        purchase.reader = reader
        purchase.presented_event = None
        purchase.presented_at = None
        purchase.waiting_since = None
        purchase.save(
            update_fields=[
                "reader",
                "presented_event",
                "presented_at",
                "waiting_since",
                "updated_at",
            ]
        )
        notify_purchase(purchase.pk, purchase.service_position_id, "purchase_updated")
    return purchase


@transaction.atomic
def wait_for_card(*, purchase: Purchase) -> Purchase:
    """The seller pressed "Scan card to buy": the next tap on the purchase's reader (within
    PURCHASE_CARD_PRESENTATION_SECONDS) goes to this purchase. Any other purchase waiting on
    the same reader stops waiting."""
    purchase = _lock_takes_card(purchase)
    if purchase.reader_id is None:
        raise NoTillReader()
    displaced = list(
        Purchase.objects.select_for_update()
        .filter(reader_id=purchase.reader_id, waiting_since__isnull=False)
        .exclude(pk=purchase.pk)
    )
    for other in displaced:  # its screen learns it no longer gets the tap
        other.waiting_since = None
        other.save(update_fields=["waiting_since", "updated_at"])
        notify_purchase(other.pk, other.service_position_id, "purchase_updated")
    purchase.waiting_since = timezone.now()
    purchase.save(update_fields=["waiting_since", "updated_at"])
    return purchase


@transaction.atomic
def stop_waiting(*, purchase: Purchase) -> Purchase:
    """The seller closed the scan dialog: taps no longer go to this purchase."""
    purchase = _lock_takes_card(purchase)
    if purchase.waiting_since is not None:
        purchase.waiting_since = None
        purchase.save(update_fields=["waiting_since", "updated_at"])
    return purchase


def _ensure_sale(purchase: Purchase, good: Good | None = None) -> None:
    """Courts are booked through their own checkout (apps.bookings), never in a till cart,
    and a booking checkout holds its one court only."""
    if purchase.kind == PurchaseKind.BOOKING:
        raise GoodNotAvailable(_("A booking checkout holds one court only."))
    if good is not None and good.kind == GoodKind.RENTAL:
        raise GoodNotAvailable(
            _("Courts are booked separately, not added to a till purchase."),
            details={"good": good.pk},
        )


@transaction.atomic
def add_item(*, purchase: Purchase, good: Good, quantity: int = 1) -> PurchaseItem:
    """Add a good, or increase its quantity if it is already in the bucket."""
    purchase = _lock_draft(purchase)
    good = Good.all_objects.select_related("service_position").get(pk=good.pk)
    _ensure_sale(purchase, good)
    _ensure_sellable(good, purchase)
    item, created = PurchaseItem.objects.get_or_create(
        purchase=purchase, good=good, defaults={"quantity": quantity}
    )
    if not created:
        item.quantity += quantity
    if item.quantity > MAX_ITEM_QUANTITY:
        raise ValidationError(
            {"quantity": [_("At most %(max)s per item.") % {"max": MAX_ITEM_QUANTITY}]}
        )
    _check_stock_hint(good, item.quantity)
    item.save()
    notify_purchase(purchase.pk, purchase.service_position_id, "purchase_updated")
    return item


@transaction.atomic
def update_item(*, purchase: Purchase, item: PurchaseItem, quantity: int) -> PurchaseItem:
    purchase = _lock_draft(purchase)
    _ensure_sale(purchase)
    _check_stock_hint(item.good, quantity)
    item.quantity = quantity
    item.save(update_fields=["quantity"])
    notify_purchase(purchase.pk, purchase.service_position_id, "purchase_updated")
    return item


@transaction.atomic
def remove_item(*, purchase: Purchase, item: PurchaseItem) -> None:
    purchase = _lock_draft(purchase)
    _ensure_sale(purchase)
    item.delete()
    notify_purchase(purchase.pk, purchase.service_position_id, "purchase_updated")


def _check_stock_hint(good: Good, quantity: int) -> None:
    """Early feedback while building the bucket; confirmation re-checks under lock."""
    if good.track_stock and quantity > good.quantity:
        raise InsufficientStock(
            details={"good": good.pk, "available": good.quantity, "requested": quantity}
        )


@transaction.atomic
def cancel_purchase(*, actor, purchase: Purchase) -> Purchase:
    purchase = _lock_draft(purchase)
    purchase.status = PurchaseStatus.CANCELLED
    purchase.cancelled_at = timezone.now()
    purchase.save(update_fields=["status", "cancelled_at", "updated_at"])
    notify_purchase(purchase.pk, purchase.service_position_id, "purchase_cancelled")
    return purchase


# --- Card presented on the till reader ---------------------------------------------------


def present_card(event) -> Purchase | None:
    """Attach an accepted TILL tap to the purchase waiting for a card on that reader.

    Only a purchase whose seller pressed "Scan card to buy" in the last
    PURCHASE_CARD_PRESENTATION_SECONDS gets the tap; old drafts never do. The tap ends the
    wait (to tap again, the seller scans again). Stale taps (replayed late) are ignored.
    Returns the purchase, or None when no purchase is waiting on this reader.
    """
    window = timedelta(seconds=settings.PURCHASE_CARD_PRESENTATION_SECONDS)
    if event.event_time < timezone.now() - window:
        return None
    with transaction.atomic():
        purchase = draft_for_reader(event.device_id, lock=True)
        if purchase is None:
            return None
        purchase.presented_event = event
        purchase.presented_at = timezone.now()
        purchase.waiting_since = None
        purchase.save(
            update_fields=["presented_event", "presented_at", "waiting_since", "updated_at"]
        )
    return purchase


def draft_for_reader(reader_id: int, lock: bool = False):
    """The purchase waiting for a card on this reader (at most one), or None: a draft to
    pay, or a paid purchase whose change costs more."""
    since = timezone.now() - timedelta(seconds=settings.PURCHASE_CARD_PRESENTATION_SECONDS)
    qs = Purchase.objects.select_for_update() if lock else Purchase.objects.all()
    return (
        qs.filter(status__in=TAKES_CARD, reader_id=reader_id, waiting_since__gte=since)
        .order_by("-waiting_since", "-id")
        .first()
    )


def _presented_uid(purchase: Purchase) -> str:
    window = timedelta(seconds=settings.PURCHASE_CARD_PRESENTATION_SECONDS)
    if (
        purchase.presented_event_id is None
        or purchase.presented_at is None
        or purchase.presented_at < timezone.now() - window
    ):
        raise CardNotPresented()
    return purchase.presented_event.uid


# --- Checkout ------------------------------------------------------------------------


def _card_holder(uid: str):
    """(card, developer, account) for a card presented at the till, or a clear error."""
    card = RFIDCard.objects.filter(uid=uid).first()
    if card is None:
        raise CardNotUsable(_("Unknown card."))
    if card.status != CardStatus.ACTIVE:
        raise CardNotUsable(
            _("This card is blocked.")
            if card.status == CardStatus.BLOCKED
            else _("This card is retired.")
        )
    assignment = (
        RFIDCardAssignment.objects.select_related("developer")
        .filter(card=card, unassigned_at__isnull=True)
        .first()
    )
    if assignment is None:
        raise CardNotUsable(_("This card is not assigned to anyone."))
    developer = assignment.developer
    if developer.deleted_at or developer.status in INACTIVE_DEVELOPER:
        raise DeveloperNotActive()
    return card, developer, finance.open_account(developer)


def confirm_purchase(
    *, actor, purchase: Purchase, pin: str, idempotency_key: str, card_uid: str | None = None
) -> tuple[Purchase, bool]:
    """Charge the card holder for a draft purchase. Returns (purchase, newly_confirmed).

    The card is the one tapped on the counter's TILL reader (`present_card`). A typed
    `card_uid` is accepted only when PURCHASE_ALLOW_MANUAL_CARD_UID is on.

    1. Resolve the card and verify the PIN (own transaction, so failures are counted).
    2. One atomic transaction, locking rows always in the same order to avoid deadlocks
       (purchase → card → account → goods by id → seller account):
       re-check card/developer/seller, check goods and stock, fix prices, deduct stock,
       debit the developer, credit the seller, mark the purchase CONFIRMED, audit.
    Any failure rolls everything back. A retry with the same Idempotency-Key returns the
    already-confirmed purchase.
    """
    replay = Purchase.objects.filter(confirm_idempotency_key=idempotency_key).first()
    if replay is not None:
        if replay.pk != purchase.pk:
            raise IdempotencyKeyReused()
        return replay, False

    presented = card_uid is None
    if presented:
        purchase = Purchase.objects.select_related("presented_event").get(pk=purchase.pk)
        card_uid = _presented_uid(purchase)
    elif not settings.PURCHASE_ALLOW_MANUAL_CARD_UID:
        raise ManualCardEntryDisabled()

    card, developer, account = _card_holder(card_uid)
    if purchase.seller.user_id and purchase.seller.user_id == developer.user_id:
        raise SelfPurchaseForbidden()
    finance.verify_pin(account=account, pin=pin)

    with transaction.atomic():
        purchase = Purchase.objects.select_for_update().get(pk=purchase.pk)
        if purchase.status != PurchaseStatus.DRAFT:
            if purchase.confirm_idempotency_key == idempotency_key:
                return purchase, False
            raise PurchaseNotDraft()
        if presented and _presented_uid(purchase) != card_uid:
            raise CardNotUsable(_("A different card was tapped meanwhile; ask for the PIN again."))

        # Re-validate under locks: the card may have been blocked since step 1.
        card = RFIDCard.objects.select_for_update().get(pk=card.pk)
        locked_card, locked_developer, _assignment = _card_holder(card.uid)
        if locked_developer.pk != developer.pk:
            raise CardNotUsable(_("The card changed owner; scan it again."))
        account = DeveloperAccount.objects.select_for_update().get(pk=account.pk)

        position = ServicePosition.all_objects.select_related("seller").get(
            pk=purchase.service_position_id
        )
        _ensure_seller_active(position)

        items = list(purchase.items.order_by("good_id"))
        if not items:
            raise PurchaseEmpty()
        locked_goods = {
            g.pk: g
            for g in Good.all_objects.select_for_update()
            .select_related("service_position")
            .filter(pk__in=[i.good_id for i in items])
            .order_by("pk")
        }

        total = Decimal("0.00")
        for item in items:
            good = locked_goods[item.good_id]
            _ensure_sellable(good, purchase, booking=item.start is not None)
            item.unit_price = good.price
            item.line_total = finance.money(good.price * item.quantity)
            total += item.line_total
        if total <= 0:
            raise PurchaseEmpty(_("The purchase total must be greater than zero."))

        reference = f"purchase:{purchase.pk}"
        for item in items:
            good = locked_goods[item.good_id]
            if good.track_stock:
                goods.move_stock(
                    good=good,
                    kind=MovementKind.SALE,
                    delta=-item.quantity,
                    actor=actor,
                    reference=reference,
                )
        PurchaseItem.objects.bulk_update(items, ["unit_price", "line_total"])

        txn = finance.post_transaction(
            account=account,
            kind=TransactionKind.PURCHASE,
            amount=-total,
            actor=actor,
            description=f"Purchase at {purchase.seller.name}",
            reference=reference,
            allow_minus=True,
        )

        purchase.status = PurchaseStatus.CONFIRMED
        purchase.developer = developer
        purchase.card = card
        purchase.total = total
        purchase.account_transaction = txn
        purchase.confirm_idempotency_key = idempotency_key
        purchase.confirmed_by = actor
        purchase.confirmed_at = timezone.now()
        # The tap paid for this checkout; a later dearer change needs a new one. The event
        # stays linked, so the tap still shows which purchase it paid.
        purchase.presented_at = None
        purchase.waiting_since = None
        purchase.save()

        booked = [i for i in items if i.start is not None]
        if booked:
            from apps.bookings import services as bookings

            bookings.book_lines(
                actor=actor,
                purchase=purchase,
                developer=developer,
                items=booked,
                goods=locked_goods,
            )

        record_audit(
            "purchase.confirmed",
            actor=actor,
            entity=purchase,
            new_values={
                "developer": developer.pk,
                "card": card.uid,
                "total": total,
                "items": [[i.good_id, i.quantity, str(i.unit_price)] for i in items],
                "bookings": [[i.good_id, i.start.isoformat(), i.quantity] for i in booked],
                "balance_after": txn.balance_after,
            },
        )
        notify_purchase(purchase.pk, purchase.service_position_id, "purchase_confirmed")
    return purchase, True


# --- Changing or deleting a paid purchase ---------------------------------------------------


def _lock_paid(purchase: Purchase) -> Purchase:
    purchase = Purchase.objects.select_for_update().get(pk=purchase.pk)
    if purchase.status != PurchaseStatus.CONFIRMED:
        raise PurchaseNotPaid()
    return purchase


def ensure_payer_present(purchase: Purchase, pin: str | None) -> None:
    """A change that costs more charges the wallet, so, as at checkout, the developer who
    paid taps their card on the counter's reader and types their PIN (verified before)."""
    if not pin:
        raise PinRequired()
    _card, holder, _account = _card_holder(_presented_uid(purchase))
    if holder.pk != purchase.developer_id:
        raise CardNotUsable(_("Tap the card of the developer who paid for this purchase."))
    if purchase.seller.user_id and purchase.seller.user_id == holder.user_id:
        raise SelfPurchaseForbidden()


def clear_presented(purchase: Purchase) -> None:
    """The tap was used: the next dearer change needs a new one. `presented_event` stays,
    so the tap still shows which purchase it paid (e.g. for a reader re-sending it)."""
    purchase.presented_at = None
    purchase.waiting_since = None
    purchase.save(update_fields=["presented_at", "waiting_since", "updated_at"])


def _lock_goods(ids) -> dict[int, Good]:
    return {
        g.pk: g
        for g in Good.all_objects.select_for_update()
        .select_related("service_position")
        .filter(pk__in=ids)
        .order_by("pk")
    }


@transaction.atomic
def refund_purchase(*, actor, purchase: Purchase) -> Purchase:
    """Delete a paid purchase: the whole total goes back to the developer's wallet (a REFUND
    entry; the ledger itself is never changed), tracked goods go back to stock, and the
    purchase becomes REFUNDED (kept for the record). A court booking is removed too, but
    only before it starts (any time if its court allows `refund_after_start`) and never
    after the developer checked in.

    Locks in the checkout's order: purchase → account → goods by id.
    """
    purchase = _lock_paid(purchase)
    account = DeveloperAccount.objects.select_for_update().get(developer_id=purchase.developer_id)
    items = list(purchase.items.order_by("good_id"))

    removed = []
    if purchase.kind == PurchaseKind.BOOKING:
        from apps.bookings.exceptions import BookingCheckedIn, BookingStarted
        from apps.bookings.models import Booking

        bookings = list(
            Booking.objects.select_for_update(of=("self",))
            .select_related("good__rental")
            .filter(purchase=purchase)
        )
        for booking in bookings:
            if booking.checked_in_at is not None:
                raise BookingCheckedIn()
            rules = getattr(booking.good, "rental", None)
            late_refund = rules is not None and rules.refund_after_start
            if booking.start <= timezone.now() and not late_refund:
                raise BookingStarted()
        for booking in bookings:
            removed.append([booking.good_id, booking.start.isoformat(), booking.slots])
            record_audit(
                "booking.deleted",
                actor=actor,
                entity=booking,
                old_values={
                    "good": booking.good_id,
                    "developer": booking.developer_id,
                    "start": booking.start.isoformat(),
                    "end": booking.end.isoformat(),
                    "purchase": purchase.pk,
                },
            )
            booking.delete()

    reference = f"purchase:{purchase.pk}"
    locked_goods = _lock_goods([i.good_id for i in items])
    for item in items:
        good = locked_goods[item.good_id]
        if good.track_stock and item.start is None:
            goods.move_stock(
                good=good,
                kind=MovementKind.RETURN,
                delta=item.quantity,
                actor=actor,
                reference=reference,
            )

    txn = finance.post_transaction(
        account=account,
        kind=TransactionKind.REFUND,
        amount=purchase.total,
        actor=actor,
        description=f"Refund: purchase {purchase.pk} at {purchase.seller.name} deleted",
        reference=reference,
    )

    purchase.status = PurchaseStatus.REFUNDED
    purchase.refunded_by = actor if getattr(actor, "pk", None) else None
    purchase.refunded_at = timezone.now()
    purchase.save(update_fields=["status", "refunded_by", "refunded_at", "updated_at"])
    record_audit(
        "purchase.refunded",
        actor=actor,
        entity=purchase,
        old_values={"status": PurchaseStatus.CONFIRMED, "total": purchase.total},
        new_values={
            "status": PurchaseStatus.REFUNDED,
            "refunded": purchase.total,
            "items": [[i.good_id, i.quantity, str(i.unit_price)] for i in items],
            "bookings": removed,
            "balance_after": txn.balance_after,
        },
    )
    notify_purchase(purchase.pk, purchase.service_position_id, "purchase_updated")
    return purchase


def change_paid_purchase(
    *, actor, purchase: Purchase, items: list[dict], pin: str | None = None
) -> Purchase:
    """Change the goods of a paid till sale: `items` is the whole new list of
    {"good", "quantity"} (a line left out is removed). Lines already paid keep the price
    charged; added goods cost their price now.

    The difference moves money at once: a lower total refunds it to the wallet (REFUND), a
    higher total charges it (PURCHASE), which needs the developer's card tap on the
    counter's reader (`wait_for_card`) and their PIN. Stock follows the
    quantities. Sending the same list again changes nothing, so a retry is safe. To remove
    every line, delete the purchase (`refund_purchase`).
    """
    if purchase.kind == PurchaseKind.BOOKING:
        raise PaidBookingNotEditable()
    if purchase.status != PurchaseStatus.CONFIRMED:
        raise PurchaseNotPaid()
    if pin:
        # Own transaction (as at checkout), so a wrong PIN is counted even if we fail later.
        finance.verify_pin(account=finance.open_account(purchase.developer), pin=pin)

    with transaction.atomic():
        purchase = _lock_paid(purchase)
        account = DeveloperAccount.objects.select_for_update().get(
            developer_id=purchase.developer_id
        )
        wanted = {line["good"].pk: line["quantity"] for line in items}
        if not wanted:
            raise PurchaseEmpty()
        current = {i.good_id: i for i in purchase.items.all()}
        locked_goods = _lock_goods(set(wanted) | set(current))
        for good_id in set(wanted) - set(current):
            good = locked_goods[good_id]
            _ensure_sale(purchase, good)
            _ensure_sellable(good, purchase)

        old_total = purchase.total
        price = {
            good_id: current[good_id].unit_price
            if good_id in current
            else locked_goods[good_id].price
            for good_id in wanted
        }
        total = sum((finance.money(price[g] * q) for g, q in wanted.items()), Decimal("0.00"))
        if total <= 0:
            raise PurchaseEmpty(_("The purchase total must be greater than zero."))
        before = {g: i.quantity for g, i in current.items()}
        if before == wanted:
            return purchase

        difference = total - old_total

        reference = f"purchase:{purchase.pk}"
        for good_id in sorted(set(wanted) | set(current)):
            good = locked_goods[good_id]
            delta = before.get(good_id, 0) - wanted.get(good_id, 0)
            if good.track_stock and delta:
                goods.move_stock(
                    good=good,
                    kind=MovementKind.RETURN if delta > 0 else MovementKind.SALE,
                    delta=delta,
                    actor=actor,
                    reference=reference,
                )

        if difference > 0:  # after stock, so a shortage shows before the card is asked for
            ensure_payer_present(purchase, pin)

        txn = None
        if difference:
            txn = finance.post_transaction(
                account=account,
                kind=TransactionKind.PURCHASE if difference > 0 else TransactionKind.REFUND,
                amount=-difference,
                actor=actor,
                description=f"Purchase {purchase.pk} at {purchase.seller.name} changed",
                reference=reference,
                allow_minus=True,
            )
            if difference > 0:
                clear_presented(purchase)

        for good_id, item in current.items():
            if good_id not in wanted:
                item.delete()
            elif item.quantity != wanted[good_id]:
                item.quantity = wanted[good_id]
                item.line_total = finance.money(item.unit_price * item.quantity)
                item.save(update_fields=["quantity", "line_total"])
        for good_id in set(wanted) - set(current):
            PurchaseItem.objects.create(
                purchase=purchase,
                good=locked_goods[good_id],
                quantity=wanted[good_id],
                unit_price=price[good_id],
                line_total=finance.money(price[good_id] * wanted[good_id]),
            )
        purchase.total = total
        purchase.save(update_fields=["total", "updated_at"])

        record_audit(
            "purchase.changed",
            actor=actor,
            entity=purchase,
            old_values={"total": old_total, "items": sorted(before.items())},
            new_values={
                "total": total,
                "items": sorted(wanted.items()),
                "difference": difference,
                "balance_after": txn.balance_after if txn else account.balance,
            },
        )
        notify_purchase(purchase.pk, purchase.service_position_id, "purchase_updated")
    return purchase
