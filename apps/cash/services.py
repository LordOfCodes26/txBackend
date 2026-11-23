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
from apps.purchases.models import Payment, Purchase, PurchaseStatus
from apps.sellers.access import acting_seller
from apps.sellers.models import Seller

from .exceptions import (
    AlreadyRequested,
    AwaitingApproval,
    CashSaleFixed,
    CashStoreLocked,
    CountResolved,
    NotReversible,
    OwnRequest,
    OwnStore,
    RequestClosed,
    StockMoved,
)
from .models import (
    CashEntry,
    CashKind,
    CashRequest,
    CountOutcome,
    RequestKind,
    RequestStatus,
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
    are checked against (prices, stock counts); staff with `permission` can."""
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


# --- Requests waiting for approval --------------------------------------------------------


def _lock_pending(request: CashRequest) -> CashRequest:
    request = CashRequest.objects.select_for_update().get(pk=request.pk)
    if request.status != RequestStatus.PENDING:
        raise RequestClosed()
    return request


def _close(request: CashRequest, status: str, *, actor=None, note: str = "") -> CashRequest:
    request.status = status
    request.decided_by = actor if getattr(actor, "pk", None) else None
    request.decided_at = timezone.now()
    request.decision_note = note.strip()
    request.save()
    return request


@transaction.atomic
def request_deletion(*, actor, purchase: Purchase, reason: str) -> CashRequest:
    """Ask finance to delete a cash sale. Someone who may approve it (and neither took the
    cash nor works for the store) deletes it at once; the request stays as the record."""
    purchase = Purchase.objects.select_for_update().get(pk=purchase.pk)
    if purchase.payment != Payment.CASH:
        raise CashSaleFixed(_("This is not a cash sale."))
    if purchase.status != PurchaseStatus.CONFIRMED:
        raise RequestClosed(_("Only a paid cash sale can be deleted."))
    if CashRequest.objects.filter(purchase=purchase, status=RequestStatus.PENDING).exists():
        raise AlreadyRequested()
    request = CashRequest.objects.create(
        kind=RequestKind.DELETE_SALE,
        seller_id=purchase.seller_id,
        purchase=purchase,
        reason=reason.strip(),
        requested_by=actor,
    )
    record_audit(
        "cash.delete_requested",
        actor=actor,
        entity=purchase,
        new_values={"total": purchase.total, "reason": request.reason, "request": request.pk},
    )
    if (
        actor.has_rbac_perm("cash.approve")
        and not works_for(actor, purchase.seller_id)
        and purchase.confirmed_by_id != actor.pk
    ):
        _carry_out(request, actor)
        _close(request, RequestStatus.APPROVED, actor=actor)
    return request


def _first_price(good: Good) -> bool:
    """A new good of a cash store that finance never approved."""
    made = CashRequest.objects.filter(good=good, kind=RequestKind.NEW_GOOD)
    return made.exists() and not made.filter(status=RequestStatus.APPROVED).exists()


@transaction.atomic
def request_price(*, actor, good: Good, price, reason: str = "") -> CashRequest | None:
    """The store wants another price: it waits for approval (a newer wish replaces it; the
    price as it is now is no wish). A good never approved yet is not on sale: its price
    changes at once, and the new price is what waits."""
    good = Good.objects.select_for_update().get(pk=good.pk)
    price = money(price)
    pending = CashRequest.objects.select_for_update().filter(
        good=good, status=RequestStatus.PENDING
    )
    if pending.filter(new_price=price).exists():
        return pending.get()
    new = _first_price(good)
    if price == good.price and not new:
        return None
    for request in pending:
        _close(request, RequestStatus.WITHDRAWN, actor=actor)
    old = None if new else good.price
    if new and price != good.price:
        goods.update_good(actor=actor, good=good, price=price)
    request = CashRequest.objects.create(
        kind=RequestKind.NEW_GOOD if new else RequestKind.PRICE,
        seller_id=good.service_position.seller_id,
        good=good,
        old_price=old,
        new_price=price,
        reason=reason.strip(),
        requested_by=actor,
    )
    record_audit(
        "cash.price_requested",
        actor=actor,
        entity=good,
        old_values={"price": old},
        new_values={"price": price, "request": request.pk},
    )
    return request


@transaction.atomic
def request_new_good(*, actor, good: Good) -> CashRequest:
    """A new good of a cash store: off sale until finance approves its price."""
    request = CashRequest.objects.create(
        kind=RequestKind.NEW_GOOD,
        seller_id=good.service_position.seller_id,
        good=good,
        new_price=good.price,
        requested_by=actor,
    )
    record_audit(
        "cash.price_requested",
        actor=actor,
        entity=good,
        new_values={"price": good.price, "request": request.pk},
    )
    return request


def create_store_good(*, actor, **data) -> Good:
    """A good made by the people of a store: at a store that takes cash it waits for
    approval (courts are paid by card and PIN only, so they don't)."""
    seller = data["service_position"].seller if data.get("service_position") else None
    if (
        seller is None
        or data.get("kind") == GoodKind.RENTAL
        or not is_locked(actor, seller, "good.create")
    ):
        return goods.create_good(actor=actor, **data)
    data["is_active"] = False
    with transaction.atomic():
        good = goods.create_good(actor=actor, **data)
        request_new_good(actor=actor, good=good)
    return good


def update_store_good(*, actor, good: Good, **changes) -> Good:
    """A good changed by the people of a store: at a store that takes cash a new price waits
    for approval, and the stock can't be taken out of the counting."""
    seller = good.service_position.seller
    if good.kind == GoodKind.RENTAL or not is_locked(actor, seller, "good.update"):
        return goods.update_good(actor=actor, good=good, **changes)
    if changes.get("track_stock", good.track_stock) != good.track_stock:
        raise CashStoreLocked()
    price = changes.pop("price", None)
    if changes.get("is_active") and not good.is_active and _first_price(good):
        raise AwaitingApproval()
    with transaction.atomic():
        if changes:
            good = goods.update_good(actor=actor, good=good, **changes)
        if price is not None:
            request_price(actor=actor, good=good, price=price)
    good.refresh_from_db()
    return good


def ensure_may_remove(actor, good: Good) -> None:
    """Removing a good with stock would hide what is missing."""
    if good.track_stock and good.quantity > 0 and good.kind != GoodKind.RENTAL:
        ensure_unlocked(actor, good.service_position.seller, "good.delete")


def ensure_may_change_stock(actor, good: Good, kind: str) -> None:
    """At a store that takes cash its people still add stock; someone else writes off and
    counts."""
    if kind != MovementKind.RESTOCK:
        ensure_unlocked(actor, good.service_position.seller, "good.stock")


def _carry_out(request: CashRequest, actor) -> None:
    from apps.purchases import services as purchases

    if request.kind == RequestKind.DELETE_SALE:
        purchases.delete_cash_sale(actor=actor, purchase=request.purchase, reason=request.reason)
    else:
        good = Good.objects.select_for_update().filter(pk=request.good_id).first()
        if good is None:
            raise RequestClosed(_("This good was removed."))
        changes = {"price": request.new_price}
        if request.kind == RequestKind.NEW_GOOD:
            changes["is_active"] = True
        goods.update_good(actor=actor, good=good, **changes)


def _ensure_may_decide(actor, request: CashRequest) -> None:
    ensure_not_own_store(actor, request.seller_id)
    if request.requested_by_id == actor.pk:
        raise OwnRequest()
    if request.purchase_id and request.purchase.confirmed_by_id == actor.pk:
        raise OwnRequest()


@transaction.atomic
def approve(*, actor, request: CashRequest, note: str = "") -> CashRequest:
    # The sale (or good) is locked before the request, as when the request is made.
    if request.purchase_id:
        Purchase.objects.select_for_update().get(pk=request.purchase_id)
    elif request.good_id:
        Good.all_objects.select_for_update().get(pk=request.good_id)
    request = _lock_pending(request)
    _ensure_may_decide(actor, request)
    _carry_out(request, actor)
    _close(request, RequestStatus.APPROVED, actor=actor, note=note)
    record_audit(
        "cash.request_approved",
        actor=actor,
        entity=request,
        new_values=_summary(request),
    )
    return request


@transaction.atomic
def reject(*, actor, request: CashRequest, note: str) -> CashRequest:
    request = _lock_pending(request)
    _ensure_may_decide(actor, request)
    _close(request, RequestStatus.REJECTED, actor=actor, note=note)
    record_audit("cash.request_rejected", actor=actor, entity=request, new_values=_summary(request))
    return request


@transaction.atomic
def withdraw(*, actor, request: CashRequest) -> CashRequest:
    """The person who asked no longer wants it. (A new good then stays off sale.)"""
    request = _lock_pending(request)
    if request.requested_by_id != actor.pk:
        raise OwnRequest(_("Only the person who asked can withdraw a request."))
    _close(request, RequestStatus.WITHDRAWN, actor=actor)
    record_audit(
        "cash.request_withdrawn", actor=actor, entity=request, new_values=_summary(request)
    )
    return request


def _summary(request: CashRequest) -> dict:
    return {
        "kind": request.kind,
        "purchase": request.purchase_id,
        "good": request.good_id,
        "price": request.new_price,
        "note": request.decision_note,
    }


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
