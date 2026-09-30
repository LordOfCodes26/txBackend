from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.finance.models import MONEY
from apps.sellers.models import Seller
from common.models import AppendOnlyModel, TimeStampedModel


class SellerAccount(TimeStampedModel):
    """What the company owes a seller. Credited by confirmed sales, debited by paid payouts.

    `balance` is a cache of the ledger; it only changes through
    `seller_finance.services.post_seller_transaction`, which locks this row.
    """

    seller = models.OneToOneField(Seller, on_delete=models.PROTECT, related_name="account")
    balance = models.DecimalField(**MONEY, default=0)

    class Meta:
        ordering = ["seller__name", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(balance__gte=0), name="seller_account_balance_not_negative"
            ),
        ]

    def __str__(self):
        return f"{self.seller} ({self.balance})"


class SellerTransactionKind(models.TextChoices):
    SALE = "SALE", "Sale"
    PAYOUT = "PAYOUT", "Payout"
    ADJUSTMENT = "ADJUSTMENT", "Manual adjustment"


class SellerTransaction(AppendOnlyModel):
    """Immutable seller ledger entry (DB trigger). `amount` is signed."""

    account = models.ForeignKey(
        SellerAccount, on_delete=models.PROTECT, related_name="transactions"
    )
    kind = models.CharField(max_length=12, choices=SellerTransactionKind.choices)
    amount = models.DecimalField(**MONEY)
    balance_after = models.DecimalField(**MONEY)
    description = models.CharField(max_length=255, blank=True)
    reference = models.CharField(max_length=100, blank=True, help_text="purchase:ID or payout:ID")
    idempotency_key = models.CharField(max_length=64, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(kind=SellerTransactionKind.SALE, amount__gt=0)
                    | Q(kind=SellerTransactionKind.PAYOUT, amount__lt=0)
                    | (Q(kind=SellerTransactionKind.ADJUSTMENT) & ~Q(amount=0))
                ),
                name="seller_txn_amount_sign_matches_kind",
            ),
            models.CheckConstraint(
                condition=Q(balance_after__gte=0), name="seller_txn_balance_not_negative"
            ),
            models.UniqueConstraint(
                "idempotency_key",
                condition=~Q(idempotency_key=""),
                name="seller_txn_idempotency_key_unique",
            ),
            # A purchase or payout is booked at most once.
            models.UniqueConstraint(
                "reference",
                condition=Q(kind__in=["SALE", "PAYOUT"]),
                name="seller_txn_reference_booked_once",
            ),
        ]
        indexes = [models.Index(fields=["account", "created_at"])]

    def __str__(self):
        return f"{self.kind} {self.amount} → {self.balance_after}"


class PayoutStatus(models.TextChoices):
    REQUESTED = "REQUESTED", "Requested"
    APPROVED = "APPROVED", "Approved"
    PROCESSING = "PROCESSING", "Processing"
    PAID = "PAID", "Paid"
    REJECTED = "REJECTED", "Rejected"
    CANCELLED = "CANCELLED", "Cancelled"


OPEN_PAYOUT_STATUSES = [PayoutStatus.REQUESTED, PayoutStatus.APPROVED, PayoutStatus.PROCESSING]


class SellerPayment(TimeStampedModel):
    """A payout of a seller's balance. Open payouts (requested/approved/processing)
    reserve their amount; the ledger is debited when the payout is marked PAID."""

    seller = models.ForeignKey(Seller, on_delete=models.PROTECT, related_name="payouts")
    amount = models.DecimalField(**MONEY)
    status = models.CharField(
        max_length=10, choices=PayoutStatus.choices, default=PayoutStatus.REQUESTED
    )
    note = models.CharField(max_length=255, blank=True)
    idempotency_key = models.CharField(max_length=64, blank=True)

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, related_name="+"
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    paid_at = models.DateTimeField(null=True, blank=True)
    payment_reference = models.CharField(
        max_length=100, blank=True, help_text="Bank transfer or cash receipt number."
    )
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=255, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    transaction = models.OneToOneField(
        SellerTransaction, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="payout_amount_positive"),
            models.CheckConstraint(
                condition=~Q(status=PayoutStatus.PAID)
                | (Q(transaction__isnull=False) & ~Q(payment_reference="")),
                name="payout_paid_is_complete",
            ),
            models.UniqueConstraint(
                "idempotency_key",
                condition=~Q(idempotency_key=""),
                name="payout_idempotency_key_unique",
            ),
        ]
        indexes = [models.Index(fields=["seller", "status"])]

    def __str__(self):
        return f"Payout {self.pk} {self.amount} ({self.status})"
