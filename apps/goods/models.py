import uuid
from pathlib import Path

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.core.validators import (
    FileExtensionValidator,
    MaxValueValidator,
    MinValueValidator,
)
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone

from apps.sellers.models import ServicePosition
from common.models import AppendOnlyModel, SoftDeleteModel, TimeStampedModel


class GoodKind(models.TextChoices):
    PRODUCT = "PRODUCT", "Product (tangible, optional stock)"
    SERVICE = "SERVICE", "Service (intangible, sold at the till)"
    RENTAL = "RENTAL", "Rental (booked by time slot)"


class GoodCategory(TimeStampedModel):
    """A store's own group of goods (Drinks, Food, ...): the till shows them as buttons.
    Each store manages its own; removing one leaves its goods without a category."""

    seller = models.ForeignKey(
        "sellers.Seller", on_delete=models.CASCADE, related_name="good_categories"
    )
    name = models.CharField(max_length=80)
    position = models.PositiveSmallIntegerField(default=0, help_text="Lowest is shown first.")

    class Meta:
        ordering = ["position", "name", "id"]
        verbose_name_plural = "good categories"
        constraints = [
            models.UniqueConstraint(Lower("name"), "seller", name="good_category_unique_name"),
        ]

    def __str__(self):
        return self.name


class Good(TimeStampedModel, SoftDeleteModel):
    """Something a seller sells at a service position.

    - PRODUCT: tangible. With `track_stock`, `quantity` is a cache of the stock ledger
      (`InventoryMovement`) and only changes through `goods.services.move_stock`.
    - SERVICE: intangible, sold at the till, no stock.
    - RENTAL: a place or thing booked by time slot (playground, pool). `price` is per
      slot; slot rules live in `RentalSettings`. Rentals are booked, never sold at the till.
    """

    service_position = models.ForeignKey(
        ServicePosition, on_delete=models.PROTECT, related_name="goods"
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    sku = models.CharField(max_length=64, blank=True, help_text="Seller's own product code.")
    price = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    kind = models.CharField(max_length=10, choices=GoodKind.choices, default=GoodKind.PRODUCT)
    # The store's category (a category of the good's own store), or none.
    category = models.ForeignKey(
        GoodCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name="goods"
    )
    is_active = models.BooleanField(default=True, help_text="Available for sale.")
    track_stock = models.BooleanField(default=True)
    quantity = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(price__gte=0), name="good_price_not_negative"),
            models.CheckConstraint(condition=Q(quantity__gte=0), name="good_quantity_not_negative"),
            models.CheckConstraint(
                condition=Q(kind=GoodKind.PRODUCT) | Q(track_stock=False),
                name="good_only_products_track_stock",
            ),
        ]
        indexes = [models.Index(fields=["service_position", "is_active"])]

    def __str__(self):
        return self.name

    @property
    def seller_id(self):
        return self.service_position.seller_id


def _all_weekdays():
    return list(range(7))


class PricePer(models.TextChoices):
    COURT = "COURT", "Per court"
    PERSON = "PERSON", "Per person"


class CourtPlace(models.TextChoices):
    INDOOR = "INDOOR", "Indoor"
    OUTDOOR = "OUTDOOR", "Outdoor"


class RentalSettings(models.Model):
    """Booking rules for a RENTAL good. Times are company-local (settings.TIME_ZONE)."""

    good = models.OneToOneField(Good, on_delete=models.CASCADE, related_name="rental")
    slot_minutes = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(15), MaxValueValidator(24 * 60)],
        help_text="Length of one bookable slot; `price` is charged per slot.",
    )
    opening_time = models.TimeField()
    closing_time = models.TimeField()
    weekdays = ArrayField(
        models.PositiveSmallIntegerField(validators=[MaxValueValidator(6)]),
        default=_all_weekdays,
        help_text="Open days: 0 = Monday ... 6 = Sunday.",
    )
    max_days_ahead = models.PositiveSmallIntegerField(
        default=30, help_text="How many days in advance a slot can be booked."
    )
    # How the price is counted: per court per slot (price x slots), or per person per slot
    # (price x people x slots; the desk says how many people, one card pays for all).
    price_per = models.CharField(max_length=6, choices=PricePer.choices, default=PricePer.COURT)
    # Indoor courts are the ones the booking check (the indoor playground's gate) checks.
    place = models.CharField(max_length=7, choices=CourtPlace.choices, default=CourtPlace.OUTDOOR)
    max_people = models.PositiveSmallIntegerField(
        default=20,
        validators=[MinValueValidator(1), MaxValueValidator(500)],
        help_text="Per person courts: at most this many people per booking.",
    )
    refund_after_start = models.BooleanField(
        default=False,
        help_text="Paid bookings can still be changed, or deleted and refunded, once they have "
        "started or ended (e.g. a no-show). Never after the developer checked in.",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(closing_time__gt=models.F("opening_time")),
                name="rental_closes_after_opening",
            ),
            models.CheckConstraint(
                condition=Q(slot_minutes__gte=15), name="rental_slot_at_least_15_min"
            ),
        ]

    def __str__(self):
        return f"{self.good} rental rules"


class CourtPrice(TimeStampedModel):
    """One of a court's prices, like a good's price: e.g. "Whole court" 20 per slot, or
    "Per person" 5 per person per slot. The desk picks one when booking; the court's
    `Good.price` follows its first active price (shown in lists). A price that was used
    is switched off, not deleted (bookings and sales point to it)."""

    good = models.ForeignKey(Good, on_delete=models.CASCADE, related_name="court_prices")
    name = models.CharField(max_length=80)
    price = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    # Per court: price x slots. Per person: price x people x slots (one card pays for all).
    per = models.CharField(max_length=6, choices=PricePer.choices, default=PricePer.COURT)
    is_active = models.BooleanField(default=True)
    position = models.PositiveSmallIntegerField(default=0, help_text="Lowest is shown first.")

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(price__gte=0), name="court_price_not_negative"),
        ]

    def __str__(self):
        return f"{self.good} · {self.name}"


def _image_path(instance, filename):
    ext = Path(filename).suffix.lower()
    return f"goods/{timezone.now():%Y/%m}/{uuid.uuid4().hex}{ext}"


class GoodImage(models.Model):
    good = models.ForeignKey(Good, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(
        upload_to=_image_path,
        validators=[FileExtensionValidator(["jpg", "jpeg", "png", "webp"])],
    )
    alt_text = models.CharField(max_length=255, blank=True)
    position = models.PositiveSmallIntegerField(default=0, help_text="Lowest is shown first.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return f"{self.good} image {self.pk}"


class MovementKind(models.TextChoices):
    INITIAL_STOCK = "INITIAL_STOCK", "Initial stock"
    RESTOCK = "RESTOCK", "Restock"
    SALE = "SALE", "Sale"
    RETURN = "RETURN", "Return"
    DAMAGE = "DAMAGE", "Damage / loss"
    ADJUSTMENT = "ADJUSTMENT", "Stock count adjustment"


class InventoryMovement(AppendOnlyModel):
    """One change to a good's stock. Immutable (DB trigger); corrections are new rows."""

    good = models.ForeignKey(Good, on_delete=models.PROTECT, related_name="movements")
    kind = models.CharField(max_length=15, choices=MovementKind.choices)
    quantity_delta = models.IntegerField()
    quantity_after = models.PositiveIntegerField()
    reason = models.CharField(max_length=255, blank=True)
    reference = models.CharField(
        max_length=100, blank=True, help_text="e.g. purchase item id for SALE/RETURN."
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(condition=~Q(quantity_delta=0), name="movement_delta_not_zero"),
        ]
        indexes = [models.Index(fields=["good", "created_at"])]

    def __str__(self):
        return f"{self.good} {self.kind} {self.quantity_delta:+d}"
