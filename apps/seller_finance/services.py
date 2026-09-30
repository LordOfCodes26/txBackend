from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import DecimalField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.audit.services import record_audit
from apps.finance.exceptions import IdempotencyKeyReused
from apps.finance.services import money
from apps.sellers.models import Seller

from .exceptions import (
    InsufficientSellerBalance,
    InvalidPayoutTransition,
    OwnSellerForbidden,
    SelfApprovalForbidden,
)
from .models import (
    OPEN_PAYOUT_STATUSES,
    PayoutStatus,
    SellerAccount,
    SellerPayment,
    SellerTransaction,
    SellerTransactionKind,
)

ZERO = Decimal("0.00")


def open_seller_account(seller: Seller) -> SellerAccount:
    account, _ = SellerAccount.objects.get_or_create(seller=seller)
    return account


def reserved_amount(seller_id: int) -> Decimal:
    """Money held by open payouts (requested, approved or processing)."""
    total = SellerPayment.objects.filter(
        seller_id=seller_id, status__in=OPEN_PAYOUT_STATUSES
    ).aggregate(s=Sum("amount"))["s"]
    return total or ZERO


def with_reserved(queryset):
    """Annotate SellerAccounts with `reserved` (sum of open payouts)."""
    open_sum = (
        SellerPayment.objects.filter(seller=OuterRef("seller"), status__in=OPEN_PAYOUT_STATUSES)
        .values("seller")
        .annotate(s=Sum("amount"))
        .values("s")
    )
    return queryset.annotate(
        reserved=Coalesce(
            Subquery(open_sum, output_field=DecimalField(max_digits=14, decimal_places=2)),
            Value(ZERO),
        )
    )


def post_seller_transaction(
    *,
    account: SellerAccount,
    kind: str,
    amount,
    actor=None,
    description: str = "",
    reference: str = "",
    idempotency_key: str = "",
) -> SellerTransaction:
    """The only way a seller balance changes. Must run inside a transaction; locks the
    account row so sales, payouts and adjustments apply one at a time."""
    amount = money(amount)
    account = SellerAccount.objects.select_for_update().get(pk=account.pk)
    new_balance = account.balance + amount
    if new_balance < 0:
        raise InsufficientSellerBalance(details={"balance": str(account.balance)})
    account.balance = new_balance
    account.save(update_fields=["balance", "updated_at"])
    return SellerTransaction.objects.create(
        account=account,
        kind=kind,
        amount=amount,
        balance_after=new_balance,
        description=description,
        reference=reference,
        idempotency_key=idempotency_key,
        actor=actor if getattr(actor, "pk", None) else None,
    )


def credit_sale(*, seller: Seller, amount, reference: str, actor=None) -> SellerTransaction:
    """Credit a confirmed purchase to its seller (100%, no commission). Called inside the
    checkout transaction, so the developer debit and the seller credit commit together."""
    return post_seller_transaction(
        account=open_seller_account(seller),
        kind=SellerTransactionKind.SALE,
        amount=amount,
        actor=actor,
        description="Sale",
        reference=reference,
    )


def _ensure_not_own_seller(actor, seller: Seller) -> None:
    if seller.user_id is not None and seller.user_id == getattr(actor, "pk", None):
        raise OwnSellerForbidden()


def _lock(payment: SellerPayment) -> tuple[SellerAccount, SellerPayment]:
    # Lock order for payouts: seller account, then payment.
    account = SellerAccount.objects.select_for_update().get(seller_id=payment.seller_id)
    return account, SellerPayment.objects.select_for_update().get(pk=payment.pk)


def _available_for_update(account: SellerAccount) -> Decimal:
    return account.balance - reserved_amount(account.seller_id)


# --- Payouts ---------------------------------------------------------------------------


def request_payout(
    *, actor, seller: Seller, amount, idempotency_key: str, note: str = ""
) -> tuple[SellerPayment, bool]:
    amount = money(amount)
    previous = SellerPayment.objects.filter(idempotency_key=idempotency_key).first()
    if previous:
        if (previous.seller_id, previous.amount) != (seller.pk, amount):
            raise IdempotencyKeyReused()
        return previous, False
    try:
        with transaction.atomic():
            account = SellerAccount.objects.select_for_update().get(
                pk=open_seller_account(seller).pk
            )
            available = _available_for_update(account)
            if amount > available:
                raise InsufficientSellerBalance(details={"available": str(available)})
            payment = SellerPayment.objects.create(
                seller=seller,
                amount=amount,
                note=note,
                idempotency_key=idempotency_key,
                requested_by=actor,
            )
            record_audit(
                "seller.payout_requested",
                actor=actor,
                entity=payment,
                new_values={"seller": seller.pk, "amount": amount, "note": note},
            )
    except IntegrityError:
        previous = SellerPayment.objects.filter(idempotency_key=idempotency_key).first()
        if previous is None:
            raise
        return previous, False
    return payment, True


def _move(payment, *, allowed, target, actor, audit_action, extra=None) -> SellerPayment:
    if payment.status not in allowed:
        raise InvalidPayoutTransition(
            details={"status": payment.status, "allowed_from": sorted(allowed)}
        )
    old = payment.status
    payment.status = target
    payment.save()
    record_audit(
        audit_action,
        actor=actor,
        entity=payment,
        old_values={"status": old},
        new_values={"status": target, **(extra or {})},
    )
    return payment


@transaction.atomic
def approve_payout(*, actor, payment: SellerPayment) -> SellerPayment:
    _, payment = _lock(payment)
    _ensure_not_own_seller(actor, payment.seller)
    if payment.requested_by_id == getattr(actor, "pk", None):
        raise SelfApprovalForbidden()
    payment.approved_by = actor
    payment.approved_at = timezone.now()
    return _move(
        payment,
        allowed={PayoutStatus.REQUESTED},
        target=PayoutStatus.APPROVED,
        actor=actor,
        audit_action="seller.payout_approved",
    )


@transaction.atomic
def mark_processing(*, actor, payment: SellerPayment) -> SellerPayment:
    _, payment = _lock(payment)
    payment.processed_at = timezone.now()
    return _move(
        payment,
        allowed={PayoutStatus.APPROVED},
        target=PayoutStatus.PROCESSING,
        actor=actor,
        audit_action="seller.payout_processing",
    )


@transaction.atomic
def pay_payout(*, actor, payment: SellerPayment, payment_reference: str) -> SellerPayment:
    """Money has left the company: debit the seller ledger and close the payout."""
    account, payment = _lock(payment)
    _ensure_not_own_seller(actor, payment.seller)
    if payment.status not in {PayoutStatus.APPROVED, PayoutStatus.PROCESSING}:
        raise InvalidPayoutTransition(details={"status": payment.status})
    txn = post_seller_transaction(
        account=account,
        kind=SellerTransactionKind.PAYOUT,
        amount=-payment.amount,
        actor=actor,
        description=f"Payout ({payment_reference})",
        reference=f"payout:{payment.pk}",
    )
    payment.transaction = txn
    payment.paid_by = actor
    payment.paid_at = timezone.now()
    payment.payment_reference = payment_reference
    return _move(
        payment,
        allowed={PayoutStatus.APPROVED, PayoutStatus.PROCESSING},
        target=PayoutStatus.PAID,
        actor=actor,
        audit_action="seller.payout_paid",
        extra={"payment_reference": payment_reference, "balance_after": txn.balance_after},
    )


@transaction.atomic
def reject_payout(*, actor, payment: SellerPayment, reason: str) -> SellerPayment:
    _, payment = _lock(payment)
    payment.rejected_by = actor
    payment.rejected_at = timezone.now()
    payment.rejection_reason = reason
    return _move(
        payment,
        allowed=set(OPEN_PAYOUT_STATUSES),
        target=PayoutStatus.REJECTED,
        actor=actor,
        audit_action="seller.payout_rejected",
        extra={"reason": reason},
    )


@transaction.atomic
def cancel_payout(*, actor, payment: SellerPayment) -> SellerPayment:
    _, payment = _lock(payment)
    payment.cancelled_at = timezone.now()
    return _move(
        payment,
        allowed={PayoutStatus.REQUESTED},
        target=PayoutStatus.CANCELLED,
        actor=actor,
        audit_action="seller.payout_cancelled",
    )


# --- Adjustments -------------------------------------------------------------------------


def adjust_seller(
    *, actor, seller: Seller, amount, reason: str, idempotency_key: str
) -> tuple[SellerTransaction, bool]:
    """Signed correction. A debit cannot eat into money reserved by open payouts."""
    amount = money(amount)
    _ensure_not_own_seller(actor, seller)
    previous = SellerTransaction.objects.filter(idempotency_key=idempotency_key).first()
    if previous:
        if (previous.account.seller_id, previous.amount) != (seller.pk, amount):
            raise IdempotencyKeyReused()
        return previous, False
    try:
        with transaction.atomic():
            account = SellerAccount.objects.select_for_update().get(
                pk=open_seller_account(seller).pk
            )
            available = _available_for_update(account)
            if amount < 0 and -amount > available:
                raise InsufficientSellerBalance(details={"available": str(available)})
            txn = post_seller_transaction(
                account=account,
                kind=SellerTransactionKind.ADJUSTMENT,
                amount=amount,
                actor=actor,
                description=reason,
                idempotency_key=idempotency_key,
            )
            record_audit(
                "seller.balance_adjusted",
                actor=actor,
                entity=account,
                old_values={"balance": txn.balance_after - amount},
                new_values={"balance": txn.balance_after, "amount": amount, "reason": reason},
            )
    except IntegrityError:
        previous = SellerTransaction.objects.filter(idempotency_key=idempotency_key).first()
        if previous is None:
            raise
        return previous, False
    return txn, True


# --- Reconciliation --------------------------------------------------------------------


def seller_ledger_mismatches() -> list[dict]:
    """Sellers whose balance != ledger sum, or whose SALE credits != confirmed purchases."""
    from apps.purchases.models import Purchase, PurchaseStatus

    last = SellerTransaction.objects.filter(account=OuterRef("pk")).order_by("-created_at", "-id")
    sales = (
        Purchase.objects.filter(seller=OuterRef("seller"), status=PurchaseStatus.CONFIRMED)
        .values("seller")
        .annotate(s=Sum("total"))
        .values("s")
    )
    rows = SellerAccount.objects.annotate(
        ledger=Sum("transactions__amount"),
        credited=Sum("transactions__amount", filter=Q(transactions__kind="SALE")),
        last_balance=Subquery(last.values("balance_after")[:1]),
        purchases=Subquery(sales, output_field=DecimalField(max_digits=14, decimal_places=2)),
    ).values("id", "seller_id", "balance", "ledger", "credited", "last_balance", "purchases")
    problems = []
    for r in rows:
        ledger, credited = r["ledger"] or ZERO, r["credited"] or ZERO
        last_balance = r["last_balance"] if r["last_balance"] is not None else ZERO
        purchases = r["purchases"] or ZERO
        if not (r["balance"] == ledger == last_balance and credited == purchases):
            normalized = r | {
                "ledger": ledger,
                "credited": credited,
                "last_balance": last_balance,
                "purchases": purchases,
            }
            problems.append(
                {k: str(v) if isinstance(v, Decimal) else v for k, v in normalized.items()}
            )
    return problems
