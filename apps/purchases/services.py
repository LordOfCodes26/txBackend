from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.audit.services import record_audit
from apps.developers.models import DeveloperStatus
from apps.finance import services as finance
from apps.finance.exceptions import IdempotencyKeyReused
from apps.finance.models import DeveloperAccount, TransactionKind
from apps.goods import services as goods
from apps.goods.exceptions import InsufficientStock
from apps.goods.models import Good, MovementKind
from apps.rfid.models import CardStatus, RFIDCard, RFIDCardAssignment
from apps.seller_finance.services import credit_sale
from apps.sellers.models import SellerStatus, ServicePosition

from .exceptions import (
    CardNotUsable,
    DeveloperNotActive,
    GoodNotAvailable,
    PurchaseEmpty,
    PurchaseNotDraft,
    SelfPurchaseForbidden,
    SellerNotActive,
)
from .models import Purchase, PurchaseItem, PurchaseStatus

INACTIVE_DEVELOPER = {DeveloperStatus.SUSPENDED, DeveloperStatus.TERMINATED}


def _lock_draft(purchase: Purchase) -> Purchase:
    purchase = Purchase.objects.select_for_update().get(pk=purchase.pk)
    if purchase.status != PurchaseStatus.DRAFT:
        raise PurchaseNotDraft()
    return purchase


def _ensure_sellable(good: Good, purchase: Purchase) -> None:
    if (
        good.deleted_at is not None
        or not good.is_active
        or good.service_position.seller_id != purchase.seller_id
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


@transaction.atomic
def create_purchase(*, actor, service_position: ServicePosition) -> Purchase:
    _ensure_seller_active(service_position)
    return Purchase.objects.create(
        seller=service_position.seller, service_position=service_position, created_by=actor
    )


@transaction.atomic
def add_item(*, purchase: Purchase, good: Good, quantity: int) -> PurchaseItem:
    """Add a good, or increase its quantity if it is already in the bucket."""
    purchase = _lock_draft(purchase)
    good = Good.all_objects.select_related("service_position").get(pk=good.pk)
    _ensure_sellable(good, purchase)
    item, created = PurchaseItem.objects.get_or_create(
        purchase=purchase, good=good, defaults={"quantity": quantity}
    )
    if not created:
        item.quantity += quantity
    _check_stock_hint(good, item.quantity)
    item.save()
    return item


@transaction.atomic
def update_item(*, purchase: Purchase, item: PurchaseItem, quantity: int) -> PurchaseItem:
    _lock_draft(purchase)
    _check_stock_hint(item.good, quantity)
    item.quantity = quantity
    item.save(update_fields=["quantity"])
    return item


@transaction.atomic
def remove_item(*, purchase: Purchase, item: PurchaseItem) -> None:
    _lock_draft(purchase)
    item.delete()


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
    return purchase


# --- Checkout ------------------------------------------------------------------------


def _card_holder(uid: str):
    """(card, developer, account) for a card presented at the till, or a clear error."""
    card = RFIDCard.objects.filter(uid=uid).first()
    if card is None:
        raise CardNotUsable("Unknown card.")
    if card.status != CardStatus.ACTIVE:
        raise CardNotUsable(f"This card is {card.status.lower()}.")
    assignment = (
        RFIDCardAssignment.objects.select_related("developer")
        .filter(card=card, unassigned_at__isnull=True)
        .first()
    )
    if assignment is None:
        raise CardNotUsable("This card is not assigned to anyone.")
    developer = assignment.developer
    if developer.deleted_at or developer.status in INACTIVE_DEVELOPER:
        raise DeveloperNotActive()
    return card, developer, finance.open_account(developer)


def confirm_purchase(
    *, actor, purchase: Purchase, card_uid: str, pin: str, idempotency_key: str
) -> tuple[Purchase, bool]:
    """Charge the card holder for a draft purchase. Returns (purchase, newly_confirmed).

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

        # Re-validate under locks: the card may have been blocked since step 1.
        card = RFIDCard.objects.select_for_update().get(pk=card.pk)
        locked_card, locked_developer, _ = _card_holder(card.uid)
        if locked_developer.pk != developer.pk:
            raise CardNotUsable("The card changed owner; scan it again.")
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
            _ensure_sellable(good, purchase)
            item.unit_price = good.price
            item.line_total = finance.money(good.price * item.quantity)
            total += item.line_total
        if total <= 0:
            raise PurchaseEmpty("The purchase total must be greater than zero.")

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
        )
        # 100% of the sale goes to the seller (no commission).
        credit_sale(seller=position.seller, amount=total, reference=reference, actor=actor)

        purchase.status = PurchaseStatus.CONFIRMED
        purchase.developer = developer
        purchase.card = card
        purchase.total = total
        purchase.account_transaction = txn
        purchase.confirm_idempotency_key = idempotency_key
        purchase.confirmed_by = actor
        purchase.confirmed_at = timezone.now()
        purchase.save()

        record_audit(
            "purchase.confirmed",
            actor=actor,
            entity=purchase,
            new_values={
                "developer": developer.pk,
                "card": card.uid,
                "total": total,
                "items": [[i.good_id, i.quantity, str(i.unit_price)] for i in items],
                "balance_after": txn.balance_after,
            },
        )
    return purchase, True
