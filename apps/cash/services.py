from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError

from apps.audit.services import record_audit
from apps.finance.exceptions import IdempotencyKeyReused
from apps.finance.services import money
from apps.goods import services as goods
from apps.goods.models import Good, GoodKind, MovementKind
from apps.purchases.models import Payment, Purchase
from apps.sellers.access import acting_seller
from apps.sellers.models import Seller

from .exceptions import (
    CashSaleFixed,
    CashStoreLocked,
    CountResolved,
    NotReversible,
    OwnSale,
    OwnStore,
    StockMoved,
)
from .models import (
    CashEntry,
    CashKind,
    CountOutcome,
    StockCount,
    StockCountLine,
)

ZERO = Decimal("0.00")


# --- Who may do what at a store that takes cash -------------------------------------------


def works_for(user, seller_id: int) -> bool:
    """The store's own login, or the manager of one of its counters."""
    seller = acting_seller(user)
    if seller is not None and seller.pk == seller_id:
        return True
    pk = getattr(user, "pk", None)
    return pk is not None and (
        Seller.objects.filter(pk=seller_id, user_id=pk).exists()
        or Seller.objects.filter(
            pk=seller_id, positions__manager_id=pk, positions__deleted_at__isnull=True
        ).exists()
    )


def is_locked(actor, seller: Seller, permission: str) -> bool:
    """While a store takes cash, the people who work for it can't change what its takings
    are checked against (stock counts); staff with `permission` can."""
    if not seller.accepts_cash:
        return False
    return works_for(actor, seller.pk) or not actor.has_rbac_perm(permission)


def ensure_unlocked(actor, seller: Seller, permission: str) -> None:
    if is_locked(actor, seller, permission):
        raise CashStoreLocked()


def ensure_not_own_store(actor, seller_id: int) -> None:
    """Finance's controls are never used on a store one works for."""
    if works_for(actor, seller_id):
        raise OwnStore()


# --- The ledger ---------------------------------------------------------------------------


def owed(seller_id: int) -> Decimal:
    """The cash the store holds and has not handed over yet."""
    total = CashEntry.objects.filter(seller_id=seller_id).aggregate(t=Sum("amount"))["t"]
    return total or ZERO


def post_entry(
    *,
    seller_id: int,
    kind: str,
    amount,
    actor=None,
    note: str = "",
    reference: str = "",
    idempotency_key: str = "",
    reverses: CashEntry | None = None,
) -> CashEntry:
    """The only way a store's owed cash changes. Must run inside a transaction.

    Locks the store's row, so entries are applied one after another and each `owed_after`
    is right. Callers lock the purchase and goods first (checkout's order), the store last.
    """
    Seller.objects.select_for_update().get(pk=seller_id)
    amount = money(amount)
    return CashEntry.objects.create(
        seller_id=seller_id,
        kind=kind,
        amount=amount,
        owed_after=owed(seller_id) + amount,
        note=note,
        reference=reference,
        idempotency_key=idempotency_key,
        reverses=reverses,
        actor=actor if getattr(actor, "pk", None) else None,
    )


def ledger_mismatches() -> list[CashEntry]:
    """Entries whose `owed_after` is not the sum up to them (the books don't add up)."""
    running: dict[int, Decimal] = {}
    wrong = []
    for entry in CashEntry.objects.order_by("id").iterator():
        running[entry.seller_id] = running.get(entry.seller_id, ZERO) + entry.amount
        if running[entry.seller_id] != entry.owed_after:
            wrong.append(entry)
    return wrong


def _replayed(key: str, *, kind: str, seller_id: int, amount=None, reverses=None):
    """The entry an earlier request with this key made, if it was this same request."""
    entry = CashEntry.objects.filter(idempotency_key=key).first()
    if entry is None:
        return None
    if (
        entry.kind != kind
        or entry.seller_id != seller_id
        or entry.reverses_id != getattr(reverses, "pk", None)
        or (amount is not None and entry.amount != money(amount))
    ):
        raise IdempotencyKeyReused()
    return entry


@transaction.atomic
def hand_over(
    *, actor, seller: Seller, amount, note: str = "", idempotency_key: str
) -> tuple[CashEntry, bool]:
    """Finance counted and took cash from the store. Returns (entry, newly made). More than
    the store owes is possible (an unrecorded sale?), but needs a note saying why."""
    ensure_not_own_store(actor, seller.pk)
    amount = money(amount)
    Seller.objects.select_for_update().get(pk=seller.pk)
    previous = _replayed(
        idempotency_key, kind=CashKind.HANDOVER, seller_id=seller.pk, amount=-amount
    )
    if previous is not None:
        return previous, False
    note = note.strip()
    before = owed(seller.pk)
    if amount > before and not note:
        raise ValidationError(
            {"note": [_("This is more than the store owes. Write why in the note.")]}
        )
    entry = post_entry(
        seller_id=seller.pk,
        kind=CashKind.HANDOVER,
        amount=-amount,
        actor=actor,
        note=note,
        idempotency_key=idempotency_key,
    )
    record_audit(
        "cash.handed_over",
        actor=actor,
        entity=seller,
        old_values={"owed": before},
        new_values={"amount": amount, "owed": entry.owed_after, "note": note, "entry": entry.pk},
    )
    return entry, True


@transaction.atomic
def reverse_entry(
    *, actor, entry: CashEntry, reason: str, idempotency_key: str
) -> tuple[CashEntry, bool]:
    """Cancel a handover or a charge entered by mistake with the opposite entry."""
    ensure_not_own_store(actor, entry.seller_id)
    count = None
    if entry.kind == CashKind.MISSING:
        # Back to "to be explained". (Locked before the store, as when it was charged.)
        count = StockCount.objects.select_for_update().filter(charge=entry).first()
    Seller.objects.select_for_update().get(pk=entry.seller_id)
    previous = _replayed(
        idempotency_key, kind=entry.kind, seller_id=entry.seller_id, reverses=entry
    )
    if previous is not None:
        return previous, False
    if (
        entry.kind not in (CashKind.HANDOVER, CashKind.MISSING)
        or entry.reverses_id is not None
        or CashEntry.objects.filter(reverses=entry).exists()
    ):
        raise NotReversible()
    back = post_entry(
        seller_id=entry.seller_id,
        kind=entry.kind,
        amount=-entry.amount,
        actor=actor,
        note=reason.strip(),
        reference=f"reverses:{entry.pk}",
        idempotency_key=idempotency_key,
        reverses=entry,
    )
    if count is not None:
        count.outcome = CountOutcome.OPEN
        count.charge = None
        count.outcome_note = ""
        count.resolved_by = None
        count.resolved_at = None
        count.save()
    record_audit(
        "cash.entry_reversed",
        actor=actor,
        entity=entry,
        old_values={"kind": entry.kind, "amount": entry.amount},
        new_values={"reason": reason, "owed": back.owed_after, "entry": back.pk},
    )
    return back, True


# --- Deleting a sale, and the store's goods ---------------------------------------------------


def delete_sale(*, actor, purchase: Purchase, reason: str) -> Purchase:
    """Finance deletes a cash sale, saying why: not for a store they work for, nor a sale they
    took the cash for."""
    from apps.purchases import services as purchases

    if purchase.payment != Payment.CASH:
        raise CashSaleFixed(_("This is not a cash sale."))
    ensure_not_own_store(actor, purchase.seller_id)
    if purchase.confirmed_by_id == actor.pk:
        raise OwnSale()
    return purchases.delete_cash_sale(actor=actor, purchase=purchase, reason=reason.strip())


def ensure_may_update(actor, good: Good, changes: dict) -> None:
    """At a store that takes cash its people may not take a good out of the stock counting."""
    if (
        good.kind != GoodKind.RENTAL
        and changes.get("track_stock", good.track_stock) != good.track_stock
    ):
        ensure_unlocked(actor, good.service_position.seller, "good.update")


def ensure_may_remove(actor, good: Good) -> None:
    """Removing a good with stock would hide what is missing."""
    if good.track_stock and good.quantity > 0 and good.kind != GoodKind.RENTAL:
        ensure_unlocked(actor, good.service_position.seller, "good.delete")


def ensure_may_change_stock(actor, good: Good, kind: str) -> None:
    """At a store that takes cash its people still add stock; someone else writes off and
    counts."""
    if kind != MovementKind.RESTOCK:
        ensure_unlocked(actor, good.service_position.seller, "good.stock")


# --- Stock counts -------------------------------------------------------------------------


def countable(seller_id: int):
    """The store's goods with stock to count (inactive ones too: they may hold stock)."""
    return Good.objects.filter(
        service_position__seller_id=seller_id, track_stock=True
    ).select_related("service_position", "category")


@transaction.atomic
def count_stock(*, actor, seller: Seller, lines: list[dict], note: str = "") -> StockCount:
    """Record counted goods: [{"good", "expected", "counted"}]. `expected` is the stock the
    counter saw; a good sold or moved since must be counted again. Differences go to stock
    as count adjustments."""
    ensure_not_own_store(actor, seller.pk)
    by_good = {line["good"].pk: line for line in lines}
    locked = {
        g.pk: g
        for g in Good.objects.select_for_update()
        .filter(pk__in=by_good, service_position__seller_id=seller.pk, track_stock=True)
        .order_by("pk")
    }
    if len(locked) != len(by_good):
        raise ValidationError({"lines": [_("Pick goods of this store that track stock.")]})
    moved = [pk for pk, good in locked.items() if good.quantity != by_good[pk]["expected"]]
    if moved:
        raise StockMoved(details={"goods": moved})
    for pk, good in locked.items():
        goods.check_quantity(good, by_good[pk]["counted"], "lines")

    count = StockCount.objects.create(seller=seller, counted_by=actor, note=note.strip())
    missing, surplus = ZERO, ZERO
    for pk, good in sorted(locked.items()):
        counted = by_good[pk]["counted"]
        StockCountLine.objects.create(
            count=count, good=good, expected=good.quantity, counted=counted, price=good.price
        )
        delta = counted - good.quantity
        if delta == 0:
            continue
        value = money(abs(delta) * good.price)
        if delta < 0:
            missing += value
        else:
            surplus += value
        goods.move_stock(
            good=good,
            kind=MovementKind.ADJUSTMENT,
            delta=delta,
            actor=actor,
            reason=f"Stock count {count.pk}",
            reference=f"stock_count:{count.pk}",
        )
    count.missing_value = missing
    count.surplus_value = surplus
    if missing > 0:
        count.outcome = CountOutcome.OPEN
    count.save()
    record_audit(
        "cash.stock_counted",
        actor=actor,
        entity=count,
        new_values={
            "seller": seller.pk,
            "goods": len(locked),
            "missing_value": missing,
            "surplus_value": surplus,
        },
    )
    return count


@transaction.atomic
def resolve_count(*, actor, count: StockCount, outcome: str, note: str, amount=None) -> StockCount:
    """The missing goods were explained (why: the note), or the store pays for them: the
    amount (all of the missing value unless less is given) is added to what it owes."""
    count = StockCount.objects.select_for_update().get(pk=count.pk)
    ensure_not_own_store(actor, count.seller_id)
    if count.outcome != CountOutcome.OPEN:
        raise CountResolved()
    if outcome == CountOutcome.CHARGED:
        amount = money(amount) if amount is not None else count.missing_value
        if not 0 < amount <= count.missing_value:
            raise ValidationError(
                {"amount": [_("Enter an amount up to the value of the missing goods.")]}
            )
        count.charge = post_entry(
            seller_id=count.seller_id,
            kind=CashKind.MISSING,
            amount=amount,
            actor=actor,
            note=note.strip(),
            reference=f"stock_count:{count.pk}",
        )
    count.outcome = outcome
    count.outcome_note = note.strip()
    count.resolved_by = actor
    count.resolved_at = timezone.now()
    count.save()
    record_audit(
        "cash.count_resolved",
        actor=actor,
        entity=count,
        new_values={
            "outcome": outcome,
            "note": count.outcome_note,
            "amount": count.charge.amount if count.charge_id else None,
        },
    )
    return count
