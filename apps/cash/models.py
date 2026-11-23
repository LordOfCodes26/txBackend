"""Cash taken at stores that sell to people without a card (`Seller.accepts_cash`).

The cash is the store's to hand over: `CashEntry` is its ledger (like a wallet's), and the sum
of a store's entries is what it owes now. Only finance records handovers and deletes cash
sales. Price changes and new goods are listed from the audit log (`reports`).
"""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.finance.models import MONEY
from apps.sellers.models import Seller
from common.models import AppendOnlyModel


class CashKind(models.TextChoices):
    SALE = "SALE", "Cash sale"
    RETURN = "RETURN", "Cash returned (sale deleted)"
    HANDOVER = "HANDOVER", "Cash handed to finance"


class CashEntry(AppendOnlyModel):
    """One change of the cash a store owes. Immutable (DB trigger); a handover
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
