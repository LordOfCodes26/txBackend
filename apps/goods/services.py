from django.conf import settings
from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError

from apps.audit.services import diff, record_audit, snapshot

from .exceptions import (
    InsufficientStock,
    LastCourtPrice,
    NoStockChange,
    StockNotTracked,
    TooManyImages,
)
from .models import (
    CourtPrice,
    Good,
    GoodCategory,
    GoodImage,
    GoodKind,
    InventoryMovement,
    MovementKind,
    PricePer,
    RentalSettings,
    is_whole,
)

GOOD_FIELDS = [
    "category_id",
    "service_position",
    "name",
    "kind",
    "description",
    "sku",
    "price",
    "is_active",
    "track_stock",
    "decimal_quantity",
]
# Manual kinds are audited; SALE/RETURN are recorded by the purchase flow itself.
MANUAL_KINDS = {
    MovementKind.INITIAL_STOCK,
    MovementKind.RESTOCK,
    MovementKind.DAMAGE,
    MovementKind.ADJUSTMENT,
}


def check_quantity(good: Good, quantity, field: str = "quantity") -> None:
    """Whole units only, unless the good is sold in fractions (`decimal_quantity`)."""
    if not good.decimal_quantity and not is_whole(quantity):
        raise ValidationError(
            {field: [_("%(good)s is counted in whole units.") % {"good": good.name}]}
        )


def move_stock(
    *, good: Good, kind: str, delta, actor=None, reason: str = "", reference: str = ""
) -> InventoryMovement:
    """The only way stock changes. Must run inside a transaction.

    Locks the good row, so concurrent sales/adjustments are applied one at a time and
    stock can never go below zero. Callers locking several goods (checkout) must lock
    them in id order first to avoid deadlocks.
    """
    good = Good.all_objects.select_for_update().get(pk=good.pk)
    if not good.track_stock:
        raise StockNotTracked()
    check_quantity(good, delta)
    new_quantity = good.quantity + delta
    if new_quantity < 0:
        raise InsufficientStock(details={"available": good.quantity, "requested": -delta})

    Good.all_objects.filter(pk=good.pk).update(
        quantity=F("quantity") + delta, updated_at=timezone.now()
    )
    movement = InventoryMovement.objects.create(
        good=good,
        kind=kind,
        quantity_delta=delta,
        quantity_after=new_quantity,
        reason=reason,
        reference=reference,
        actor=actor if getattr(actor, "pk", None) else None,
    )
    if kind in MANUAL_KINDS:
        record_audit(
            "good.stock_changed",
            actor=actor,
            entity=good,
            old_values={"quantity": good.quantity},
            new_values={"quantity": new_quantity, "kind": kind, "reason": reason},
        )
    return movement


RENTAL_FIELDS = [
    "slot_minutes",
    "opening_time",
    "closing_time",
    "weekdays",
    "max_days_ahead",
    "price_per",
    "place",
    "max_people",
    "refund_after_start",
]


def _good_snapshot(good: Good) -> dict:
    values = snapshot(good, GOOD_FIELDS)
    rental = RentalSettings.objects.filter(good=good).first()
    if rental is not None:
        values["rental"] = snapshot(rental, RENTAL_FIELDS)
    return values


@transaction.atomic
def create_good(*, actor, initial_quantity=0, rental: dict | None = None, **data) -> Good:
    if data.get("service_position") is None and data.get("kind") == GoodKind.RENTAL:
        data["service_position"] = playground_position(actor)
    if data.get("kind", GoodKind.PRODUCT) != GoodKind.PRODUCT:
        data["track_stock"] = False
    good = Good.objects.create(**data)
    if rental:
        rules = RentalSettings.objects.create(good=good, **rental)
        # A new court starts with one price: its price, counted per court or per person.
        CourtPrice.objects.create(
            good=good,
            name="Per person" if rules.price_per == PricePer.PERSON else "Whole court",
            price=good.price,
            per=rules.price_per,
        )
    record_audit("good.created", actor=actor, entity=good, new_values=_good_snapshot(good))
    if initial_quantity:
        move_stock(
            good=good,
            kind=MovementKind.INITIAL_STOCK,
            delta=initial_quantity,
            actor=actor,
            reason="Initial stock",
        )
        good.refresh_from_db()
    return good


PLAYGROUND = "Playground"


def playground_position(actor):
    """The first court with no position yet: the Playground store and its desk (made once)."""
    from apps.sellers import services as sellers
    from apps.sellers.models import Seller, ServicePosition

    seller = Seller.objects.filter(name__iexact=PLAYGROUND).first() or sellers.create_seller(
        actor=actor, name=PLAYGROUND
    )
    position = ServicePosition.objects.filter(seller=seller, is_active=True).order_by("id").first()
    return position or sellers.create_position(actor=actor, seller=seller, name=PLAYGROUND)


@transaction.atomic
def update_good(*, actor, good: Good, rental: dict | None = None, **changes) -> Good:
    good = Good.objects.select_for_update().get(pk=good.pk)
    before = _good_snapshot(good)
    for field, value in changes.items():
        setattr(good, field, value)
    good.save()
    if rental and good.kind == GoodKind.RENTAL:
        settings_row = RentalSettings.objects.select_for_update().get(good=good)
        for field, value in rental.items():
            setattr(settings_row, field, value)
        settings_row.save()
    if "price" in changes and good.kind == GoodKind.RENTAL:
        # The court's price is its first price (a court's own page changes the others).
        first = CourtPrice.objects.filter(good=good, is_active=True).first()
        if first is not None and first.price != good.price:
            first.price = good.price
            first.save(update_fields=["price", "updated_at"])
    old, new = diff(before, _good_snapshot(good))
    if new:
        record_audit("good.updated", actor=actor, entity=good, old_values=old, new_values=new)
    return good


@transaction.atomic
def delete_good(*, actor, good: Good) -> None:
    good = Good.objects.select_for_update().get(pk=good.pk)
    good.deleted_at = timezone.now()
    good.is_active = False
    good.save(update_fields=["deleted_at", "is_active", "updated_at"])
    record_audit("good.deleted", actor=actor, entity=good, old_values=_good_snapshot(good))


@transaction.atomic
def adjust_stock(
    *,
    actor,
    good: Good,
    kind: str,
    quantity=None,
    counted_quantity=None,
    reason: str = "",
) -> InventoryMovement:
    """Manual stock change from the API: RESTOCK (+quantity), DAMAGE (-quantity) or
    ADJUSTMENT (set stock to a physical count)."""
    if kind == MovementKind.ADJUSTMENT:
        check_quantity(good, counted_quantity, "counted_quantity")
    else:
        check_quantity(good, quantity)
    if kind == MovementKind.RESTOCK:
        delta = quantity
    elif kind == MovementKind.DAMAGE:
        delta = -quantity
    else:
        current = Good.all_objects.select_for_update().get(pk=good.pk).quantity
        delta = counted_quantity - current
        if delta == 0:
            raise NoStockChange()
    return move_stock(good=good, kind=kind, delta=delta, actor=actor, reason=reason)


@transaction.atomic
def add_image(*, actor, good: Good, image, alt_text: str = "", position: int = 0) -> GoodImage:
    Good.objects.select_for_update().get(pk=good.pk)
    if good.images.count() >= settings.GOOD_MAX_IMAGES:
        raise TooManyImages()
    img = GoodImage.objects.create(good=good, image=image, alt_text=alt_text, position=position)
    record_audit("good.image_added", actor=actor, entity=good, new_values={"image": img.pk})
    return img


@transaction.atomic
def remove_image(*, actor, image: GoodImage) -> None:
    good, name, storage = image.good, image.image.name, image.image.storage
    image.delete()
    record_audit("good.image_removed", actor=actor, entity=good, old_values={"image": name})
    # Delete the file only once the DB change is committed.
    transaction.on_commit(lambda: storage.delete(name))


def stock_mismatches():
    """Goods whose cached quantity differs from the sum of their movements."""
    goods = (
        Good.all_objects.filter(track_stock=True)
        .annotate(ledger=Sum("movements__quantity_delta"))
        .values("id", "name", "quantity", "ledger")
    )
    return [g for g in goods if (g["ledger"] or 0) != g["quantity"]]


# --- A court's prices ----------------------------------------------------------------------

PRICE_FIELDS = ["name", "price", "per", "is_active", "position"]


def _follow_first_price(good: Good) -> None:
    """The court's own price (shown in lists) is its first active price."""
    first = CourtPrice.objects.filter(good=good, is_active=True).first()
    if first is not None and good.price != first.price:
        Good.objects.filter(pk=good.pk).update(price=first.price, updated_at=timezone.now())


@transaction.atomic
def add_court_price(*, actor, good: Good, name: str, price, per=PricePer.COURT, position=None):
    good = Good.objects.select_for_update().get(pk=good.pk)
    if position is None:
        last = CourtPrice.objects.filter(good=good).order_by("-position").first()
        position = last.position + 1 if last else 0
    row = CourtPrice.objects.create(good=good, name=name, price=price, per=per, position=position)
    _follow_first_price(good)
    record_audit(
        "court_price.created", actor=actor, entity=row, new_values=snapshot(row, PRICE_FIELDS)
    )
    return row


def _ensure_one_left(row: CourtPrice) -> None:
    if (
        not CourtPrice.objects.filter(good_id=row.good_id, is_active=True)
        .exclude(pk=row.pk)
        .exists()
    ):
        raise LastCourtPrice()


@transaction.atomic
def update_court_price(*, actor, row: CourtPrice, **changes) -> CourtPrice:
    Good.objects.select_for_update().get(pk=row.good_id)
    row = CourtPrice.objects.select_for_update().get(pk=row.pk)
    before = snapshot(row, PRICE_FIELDS)
    if changes.get("is_active") is False and row.is_active:
        _ensure_one_left(row)
    for field, value in changes.items():
        setattr(row, field, value)
    row.save()
    _follow_first_price(row.good)
    old, new = diff(before, snapshot(row, PRICE_FIELDS))
    if new:
        record_audit("court_price.updated", actor=actor, entity=row, old_values=old, new_values=new)
    return row


@transaction.atomic
def remove_court_price(*, actor, row: CourtPrice) -> None:
    """Deleted if never used; else switched off (bookings and sales keep pointing to it)."""
    from apps.bookings.models import Booking
    from apps.purchases.models import PurchaseItem

    Good.objects.select_for_update().get(pk=row.good_id)
    row = CourtPrice.objects.select_for_update().get(pk=row.pk)
    if row.is_active:
        _ensure_one_left(row)
    record_audit(
        "court_price.deleted", actor=actor, entity=row, old_values=snapshot(row, PRICE_FIELDS)
    )
    used = (
        Booking.objects.filter(court_price=row).exists()
        or PurchaseItem.objects.filter(court_price=row).exists()
    )
    if used:
        row.is_active = False
        row.save(update_fields=["is_active", "updated_at"])
    else:
        row.delete()
    _follow_first_price(row.good)


# --- A store's categories of goods -------------------------------------------------------

CATEGORY_FIELDS = ["name", "position"]


@transaction.atomic
def create_category(*, actor, seller, name: str, position: int | None = None) -> GoodCategory:
    if position is None:
        last = GoodCategory.objects.filter(seller=seller).order_by("-position").first()
        position = last.position + 1 if last else 0
    row = GoodCategory.objects.create(seller=seller, name=name.strip(), position=position)
    record_audit(
        "good_category.created",
        actor=actor,
        entity=row,
        new_values={"seller": seller.pk, **snapshot(row, CATEGORY_FIELDS)},
    )
    return row


@transaction.atomic
def update_category(*, actor, row: GoodCategory, **changes) -> GoodCategory:
    row = GoodCategory.objects.select_for_update().get(pk=row.pk)
    before = snapshot(row, CATEGORY_FIELDS)
    for field, value in changes.items():
        setattr(row, field, value.strip() if field == "name" else value)
    row.save()
    old, new = diff(before, snapshot(row, CATEGORY_FIELDS))
    if new:
        record_audit(
            "good_category.updated", actor=actor, entity=row, old_values=old, new_values=new
        )
    return row


@transaction.atomic
def delete_category(*, actor, row: GoodCategory) -> None:
    """Its goods stay, without a category."""
    record_audit(
        "good_category.deleted",
        actor=actor,
        entity=row,
        old_values={"seller": row.seller_id, **snapshot(row, CATEGORY_FIELDS)},
    )
    row.delete()
