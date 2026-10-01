from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q

from apps.developers.models import Developer
from apps.finance.models import MONEY, AccountTransaction
from apps.goods.models import Good
from apps.rfid.models import RFIDCard
from apps.sellers.models import Seller, ServicePosition
from common.models import TimeStampedModel


class PurchaseStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft (bucket being built)"
    CONFIRMED = "CONFIRMED", "Confirmed (paid)"
    CANCELLED = "CANCELLED", "Cancelled"


class Purchase(TimeStampedModel):
    """A sale at a seller's till.

    A seller builds a DRAFT bucket, then confirms it with the developer's card and PIN.
    Confirmation is one database transaction: stock, the developer's balance and the
    purchase are updated together or not at all. Confirmed purchases are final.
    """

    seller = models.ForeignKey(Seller, on_delete=models.PROTECT, related_name="purchases")
    service_position = models.ForeignKey(
        ServicePosition, on_delete=models.PROTECT, related_name="purchases"
    )
    status = models.CharField(
        max_length=10, choices=PurchaseStatus.choices, default=PurchaseStatus.DRAFT
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, related_name="+"
    )
    # The till reader the seller's PC uses for this purchase; taps on it go here.
    reader = models.ForeignKey(
        "rfid.RFIDDevice",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="purchases",
        limit_choices_to={"purpose": "TILL"},
    )
    # Address of the seller's PC that created the purchase. A tap from a reader at the same
    # address goes to this purchase when no purchase names that reader explicitly.
    client_ip = models.GenericIPAddressField(null=True, blank=True)
    # Last card tapped on that reader while the purchase was a draft.
    presented_event = models.ForeignKey(
        "rfid.RFIDEvent",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    presented_at = models.DateTimeField(null=True, blank=True)
    # Filled on confirmation.
    developer = models.ForeignKey(
        Developer, on_delete=models.PROTECT, null=True, blank=True, related_name="purchases"
    )
    card = models.ForeignKey(
        RFIDCard, on_delete=models.PROTECT, null=True, blank=True, related_name="purchases"
    )
    total = models.DecimalField(**MONEY, null=True, blank=True)
    account_transaction = models.OneToOneField(
        AccountTransaction, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    confirm_idempotency_key = models.CharField(max_length=64, blank=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                "confirm_idempotency_key",
                condition=~Q(confirm_idempotency_key=""),
                name="purchase_confirm_key_unique",
            ),
            models.CheckConstraint(
                condition=~Q(status=PurchaseStatus.CONFIRMED)
                | Q(
                    developer__isnull=False,
                    total__isnull=False,
                    account_transaction__isnull=False,
                    confirmed_at__isnull=False,
                ),
                name="purchase_confirmed_is_complete",
            ),
        ]
        indexes = [
            models.Index(fields=["reader", "status", "created_at"]),
            models.Index(fields=["client_ip", "status", "created_at"]),
            models.Index(fields=["seller", "status", "created_at"]),
            models.Index(fields=["developer", "confirmed_at"]),
        ]

    def __str__(self):
        return f"Purchase {self.pk} ({self.status})"


class PurchaseItem(models.Model):
    purchase = models.ForeignKey(Purchase, on_delete=models.CASCADE, related_name="items")
    good = models.ForeignKey(Good, on_delete=models.PROTECT, related_name="purchase_items")
    quantity = models.PositiveIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(999)]
    )
    # RENTAL lines: start of the first booked slot; `quantity` is the number of slots.
    start = models.DateTimeField(null=True, blank=True)
    # Price actually charged, fixed at confirmation (drafts show the current price).
    unit_price = models.DecimalField(**MONEY, null=True, blank=True)
    line_total = models.DecimalField(**MONEY, null=True, blank=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["purchase", "good"], name="purchase_item_good_unique"),
            models.CheckConstraint(condition=Q(quantity__gte=1), name="purchase_item_quantity_min"),
        ]

    def __str__(self):
        return f"{self.quantity} × {self.good}"
