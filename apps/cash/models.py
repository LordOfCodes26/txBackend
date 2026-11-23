"""Cash taken at stores, and what keeps it under finance's control.

Stores that accept cash (`Seller.accepts_cash`) sell to people without a card. The cash is
the store's to hand over: `CashEntry` is its ledger (like a wallet's), and the sum of a
store's entries is what it owes now. Only finance records handovers and deletes cash sales;
someone else counts the stock (`StockCount`), and missing goods are explained or charged to
the store. Price changes and new goods are listed from the audit log (`reports`).
"""

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.finance.models import MONEY
from apps.goods.models import QUANTITY, Good
from apps.sellers.models import Seller
from common.models import AppendOnlyModel, TimeStampedModel


class CashKind(models.TextChoices):
    SALE = "SALE", "Cash sale"
    RETURN = "RETURN", "Cash returned (sale deleted)"
    HANDOVER = "HANDOVER", "Cash handed to finance"
    MISSING = "MISSING", "Missing goods charged to the store"


class CashEntry(AppendOnlyModel):
    """One change of the cash a store owes. Immutable (DB trigger); a handover or charge
    entered by mistake is cancelled by an opposite entry pointing at it (`reverses`)."""

    seller = models.ForeignKey(Seller, on_delete=models.PROTECT, related_name="cash_entries")
    kind = models.CharField(max_length=10, choices=CashKind.choices)
    # Signed: what the store owes more (+) or less (-).
    amount = models.DecimalField(**MONEY)
    owed_after = models.DecimalField(**MONEY)
    note = models.CharField(max_length=255, blank=True)
    reference = models.CharField(max_length=100, blank=True, help_text="e.g. purchase:<id>.")
    idempotency_key = models.CharField(max_length=64, blank=True)
    reverses = models.OneToOneField(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="reversal"
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-id"]
        verbose_name_plural = "cash entries"
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(kind=CashKind.SALE, amount__gt=0, reverses__isnull=True)
                    | Q(kind=CashKind.RETURN, amount__lt=0, reverses__isnull=True)
                    | Q(kind=CashKind.HANDOVER, amount__lt=0, reverses__isnull=True)
                    | Q(kind=CashKind.HANDOVER, amount__gt=0, reverses__isnull=False)
                    | Q(kind=CashKind.MISSING, amount__gt=0, reverses__isnull=True)
                    | Q(kind=CashKind.MISSING, amount__lt=0, reverses__isnull=False)
                ),
                name="cash_entry_sign_matches_kind",
            ),
            models.UniqueConstraint(
                fields=["idempotency_key"],
                condition=~Q(idempotency_key=""),
                name="cash_entry_idempotency_key_unique",
            ),
            # A sale is booked once and returned at most once.
            models.UniqueConstraint(
                fields=["kind", "reference"],
                condition=Q(kind__in=("SALE", "RETURN")),
                name="cash_entry_once_per_purchase",
            ),
        ]
        indexes = [models.Index(fields=["seller", "created_at"])]

    def __str__(self):
        return f"{self.kind} {self.amount} → {self.owed_after}"


class CountOutcome(models.TextChoices):
    MATCHED = "MATCHED", "Nothing is missing"
    OPEN = "OPEN", "To be explained"
    EXPLAINED = "EXPLAINED", "Explained"
    CHARGED = "CHARGED", "Charged to the store"


class StockCount(TimeStampedModel):
    """Someone who does not work for the store counted its goods. Differences are written to
    stock at once; `missing_value` (missing units x price) is the store's to explain."""

    seller = models.ForeignKey(Seller, on_delete=models.CASCADE, related_name="stock_counts")
    counted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    note = models.CharField(max_length=255, blank=True)
    missing_value = models.DecimalField(**MONEY, default=0)
    surplus_value = models.DecimalField(**MONEY, default=0)
    outcome = models.CharField(
        max_length=10, choices=CountOutcome.choices, default=CountOutcome.MATCHED
    )
    outcome_note = models.CharField(max_length=255, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    # CHARGED: the entry that added it to what the store owes.
    charge = models.ForeignKey(
        CashEntry, on_delete=models.PROTECT, null=True, blank=True, related_name="stock_counts"
    )

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(outcome="CHARGED", charge__isnull=False)
                | (~Q(outcome="CHARGED") & Q(charge__isnull=True)),
                name="stock_count_charge_matches_outcome",
            ),
        ]
        indexes = [models.Index(fields=["seller", "outcome"])]

    def __str__(self):
        return f"Stock count {self.pk} of {self.seller}"


class StockCountLine(models.Model):
    count = models.ForeignKey(StockCount, on_delete=models.CASCADE, related_name="lines")
    good = models.ForeignKey(Good, on_delete=models.CASCADE, related_name="count_lines")
    expected = models.DecimalField(**QUANTITY)
    counted = models.DecimalField(**QUANTITY, validators=[MinValueValidator(0)])
    # The price when counted: what a missing unit is worth.
    price = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["count", "good"], name="stock_count_good_unique"),
        ]

    def __str__(self):
        return f"{self.good}: {self.counted} of {self.expected}"

    @property
    def difference(self):
        return self.counted - self.expected
