from django.conf import settings
from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from apps.audit.services import diff, record_audit, snapshot

from .exceptions import InsufficientStock, NoStockChange, StockNotTracked, TooManyImages
from .models import Good, GoodImage, GoodKind, InventoryMovement, MovementKind, RentalSettings

GOOD_FIELDS = [
    "service_position",
    "name",
    "kind",
    "description",
    "sku",
    "price",
    "is_active",
    "track_stock",
]
# Manual kinds are audited; SALE/RETURN are recorded by the purchase flow itself.
MANUAL_KINDS = {
    MovementKind.INITIAL_STOCK,
    MovementKind.RESTOCK,
    MovementKind.DAMAGE,
    MovementKind.ADJUSTMENT,
}


def move_stock(
    *, good: Good, kind: str, delta: int, actor=None, reason: str = "", reference: str = ""
) -> InventoryMovement:
    """The only way stock changes. Must run inside a transaction.

    Locks the good row, so concurrent sales/adjustments are applied one at a time and
    stock can never go below zero. Callers locking several goods (checkout) must lock
    them in id order first to avoid deadlocks.
    """
    good = Good.all_objects.select_for_update().get(pk=good.pk)
    if not good.track_stock:
        raise StockNotTracked()
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
    "max_slots_per_booking",
    "max_days_ahead",
]


def _good_snapshot(good: Good) -> dict:
    values = snapshot(good, GOOD_FIELDS)
    rental = RentalSettings.objects.filter(good=good).first()
    if rental is not None:
        values["rental"] = snapshot(rental, RENTAL_FIELDS)
    return values


@transaction.atomic
def create_good(*, actor, initial_quantity: int = 0, rental: dict | None = None, **data) -> Good:
    if data.get("kind", GoodKind.PRODUCT) != GoodKind.PRODUCT:
        data["track_stock"] = False
    good = Good.objects.create(**data)
    if rental:
        RentalSettings.objects.create(good=good, **rental)
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
    quantity: int | None = None,
    counted_quantity: int | None = None,
    reason: str = "",
) -> InventoryMovement:
    """Manual stock change from the API: RESTOCK (+quantity), DAMAGE (-quantity) or
    ADJUSTMENT (set stock to a physical count)."""
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
