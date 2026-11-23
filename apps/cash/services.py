from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError

from apps.audit.services import record_audit
from apps.finance.exceptions import IdempotencyKeyReused
from apps.finance.services import money
from apps.purchases.models import Payment, Purchase
from apps.sellers.access import acting_seller
from apps.sellers.models import Seller

from .exceptions import (
    CashSaleFixed,
    NotReversible,
    OwnSale,
    OwnStore,
)
from .models import (
    CashEntry,
    CashKind,
)

ZERO = Decimal("0.00")


# --- Not for one's own store --------------------------------------------------------------


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
    """Cancel a handover entered by mistake with the opposite entry."""
    ensure_not_own_store(actor, entry.seller_id)
    Seller.objects.select_for_update().get(pk=entry.seller_id)
    previous = _replayed(
        idempotency_key, kind=entry.kind, seller_id=entry.seller_id, reverses=entry
    )
    if previous is not None:
        return previous, False
    if (
        entry.kind != CashKind.HANDOVER
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
    record_audit(
        "cash.entry_reversed",
        actor=actor,
        entity=entry,
        old_values={"kind": entry.kind, "amount": entry.amount},
        new_values={"reason": reason, "owed": back.owed_after, "entry": back.pk},
    )
    return back, True


# --- Deleting a sale ----------------------------------------------------------------------


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
