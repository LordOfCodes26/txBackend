from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from common.models import SoftDeleteModel, TimeStampedModel


class SellerStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    SUSPENDED = "SUSPENDED", "Suspended"
    CLOSED = "CLOSED", "Closed"


class Seller(TimeStampedModel):
    """A business selling to developers. Never deleted (sales history points here);
    set `status=CLOSED` instead.

    The linked user manages this seller's own catalogue. Only ACTIVE sellers can.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="seller_profiles",
    )
    name = models.CharField(max_length=255)
    contact_name = models.CharField(max_length=255, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    status = models.CharField(
        max_length=10, choices=SellerStatus.choices, default=SellerStatus.ACTIVE
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(Lower("name"), name="seller_name_ci_unique"),
            models.UniqueConstraint(
                "user", condition=Q(user__isnull=False), name="seller_user_unique"
            ),
        ]

    def __str__(self):
        return self.name


class ServicePosition(TimeStampedModel, SoftDeleteModel):
    """A place where a seller serves: a counter, stall or till."""

    seller = models.ForeignKey(Seller, on_delete=models.PROTECT, related_name="positions")
    name = models.CharField(max_length=100)
    location = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["seller_id", "name", "id"]
        constraints = [
            models.UniqueConstraint(
                "seller",
                Lower("name"),
                condition=Q(deleted_at__isnull=True),
                name="position_name_per_seller_unique",
            ),
        ]

    def __str__(self):
        return f"{self.seller} / {self.name}"
