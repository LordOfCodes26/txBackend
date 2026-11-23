from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from apps.developers.models import Developer
from apps.finance.models import MONEY, AccountTransaction
from apps.goods.models import QUANTITY, Good, show_quantity
from apps.rfid.models import RFIDCard
from apps.sellers.models import Seller, ServicePosition
from common.models import TimeStampedModel


class PurchaseStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft (bucket being built)"
    CONFIRMED = "CONFIRMED", "Confirmed (paid)"
    CANCELLED = "CANCELLED", "Cancelled"
    REFUNDED = "REFUNDED", "Refunded (deleted after payment)"


class PurchaseKind(models.TextChoices):
    SALE = "SALE", "Sale of goods at a till"
    BOOKING = "BOOKING", "Court booking checkout"


class Payment(models.TextChoices):
    WALLET = "WALLET", "Card and PIN (the developer's wallet)"
    CASH = "CASH", "Cash (no card, developer or wallet)"


class Purchase(TimeStampedModel):
    """A sale at a seller's till.

    A seller builds a DRAFT bucket, then confirms it with the developer's card and PIN.
    Confirmation is one database transaction: stock, the developer's balance and the
    purchase are updated together or not at all. A confirmed till sale can later be
    changed (the difference is refunded or charged) or deleted (REFUNDED: the total goes
    back to the wallet and the goods back to stock); see services.change_paid_purchase /
    refund_purchase.

    A CASH sale (at a store that accepts cash) has no developer, card or wallet entry: the
    seller keeps the money. It is never changed, and only finance
    deletes it.
    """

    seller = models.ForeignKey(Seller, on_delete=models.PROTECT, related_name="purchases")
    # The store's own running number (1, 2, 3, ... per seller), given when the purchase is
    # opened, so drafts have one too and cancelled drafts leave gaps. `id` stays global.
    store_number = models.PositiveIntegerField(null=True, blank=True, editable=False)
    # The seller's own name for it (e.g. "Table 3", a customer), to tell open sales apart.
    label = models.CharField(max_length=60, blank=True)
    # A court booking reserved for free, paid by this purchase on arrival.
    booking_to_pay = models.ForeignKey(
        "bookings.Booking",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payments",
    )
    # SALE: goods at a till. BOOKING: one court booking (apps.bookings), paid the same way.
    kind = models.CharField(max_length=10, choices=PurchaseKind.choices, default=PurchaseKind.SALE)
    payment = models.CharField(max_length=6, choices=Payment.choices, default=Payment.WALLET)
    # Cash sales: who bought, if the seller typed it.
    buyer_name = models.CharField(max_length=100, blank=True)
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
        # Card assign (master) readers only for court bookings (see services.usable_readers).
        limit_choices_to={"purpose__in": ["TILL", "ENROLL"]},
    )
    # Address of the seller's PC that created the purchase (for the record).
    client_ip = models.GenericIPAddressField(null=True, blank=True)
    # Set when the seller presses "Scan card to buy": only a purchase waiting on its reader
    # (within PURCHASE_CARD_PRESENTATION_SECONDS) receives the next tap. One per reader.
    waiting_since = models.DateTimeField(null=True, blank=True)
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
    refunded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    refunded_at = models.DateTimeField(null=True, blank=True)
    # Why a cash sale was deleted.
    refund_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["seller", "store_number"], name="purchase_store_number_unique"
            ),
            models.UniqueConstraint(
                "confirm_idempotency_key",
                condition=~Q(confirm_idempotency_key=""),
                name="purchase_confirm_key_unique",
            ),
            # A purchase made entirely of free preferential units costs 0: no ledger entry.
            models.CheckConstraint(
                condition=~Q(status=PurchaseStatus.CONFIRMED)
                | Q(
                    Q(account_transaction__isnull=False) | Q(total=0),
                    payment=Payment.WALLET,
                    developer__isnull=False,
                    total__isnull=False,
                    confirmed_at__isnull=False,
                )
                | Q(
                    payment=Payment.CASH,
                    developer__isnull=True,
                    total__isnull=False,
                    total__gt=0,
                    confirmed_at__isnull=False,
                ),
                name="purchase_confirmed_is_complete",
            ),
            # Cash buys goods at a till, and never touches a wallet.
            models.CheckConstraint(
                condition=Q(payment=Payment.WALLET)
                | Q(
                    kind=PurchaseKind.SALE,
                    developer__isnull=True,
                    card__isnull=True,
                    account_transaction__isnull=True,
                ),
                name="purchase_cash_has_no_wallet",
            ),
        ]
        indexes = [
            models.Index(fields=["reader", "status", "created_at"]),
            models.Index(fields=["reader", "status", "waiting_since"]),
            models.Index(fields=["seller", "status", "created_at"]),
            models.Index(fields=["developer", "confirmed_at"]),
            models.Index(fields=["payment", "confirmed_at"]),
        ]

    def __str__(self):
        return f"Purchase {self.pk} ({self.status})"

    def save(self, *args, **kwargs):
        if not self._state.adding or self.store_number is not None:
            return super().save(*args, **kwargs)
        # Locking the store's row hands out each number once, even to tills opening
        # purchases at the same moment.
        with transaction.atomic():
            counter = (
                Seller.objects.select_for_update()
                .values_list("purchase_counter", flat=True)
                .get(pk=self.seller_id)
            ) + 1
            Seller.objects.filter(pk=self.seller_id).update(purchase_counter=counter)
            self.store_number = counter
            return super().save(*args, **kwargs)


class PurchaseItem(models.Model):
    purchase = models.ForeignKey(Purchase, on_delete=models.CASCADE, related_name="items")
    good = models.ForeignKey(Good, on_delete=models.PROTECT, related_name="purchase_items")
    # Whole units, or hundredths for a good with `decimal_quantity`.
    quantity = models.DecimalField(
        **QUANTITY, validators=[MinValueValidator(Decimal("0.01")), MaxValueValidator(999)]
    )
    # RENTAL lines: start of the first booked slot; `quantity` is the number of slots.
    start = models.DateTimeField(null=True, blank=True)
    # RENTAL lines: which of the court's prices (None: the court's own price).
    court_price = models.ForeignKey(
        "goods.CourtPrice", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    # RENTAL lines priced per person: how many people (line total = price x slots x people).
    people = models.PositiveSmallIntegerField(default=1)
    # Units given free by the preferential service, fixed at confirmation: whole units only
    # (2.5 kg with 3 items left: 2 free). Never lowered (used is used, even when the line
    # shrinks or the purchase is deleted); the line is charged for
    # max(quantity - free_quantity, 0) units.
    free_quantity = models.PositiveIntegerField(default=0)
    # Price actually charged, fixed at confirmation (drafts show the current price).
    unit_price = models.DecimalField(**MONEY, null=True, blank=True)
    line_total = models.DecimalField(**MONEY, null=True, blank=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["purchase", "good"], name="purchase_item_good_unique"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="purchase_item_quantity_min"),
        ]

    def __str__(self):
        return f"{show_quantity(self.quantity)} × {self.good}"


class PreferentialSettings(TimeStampedModel):
    """The preferential service, set by admins. One row (pk=1); see `current()`.

    `levels` are the rules: [{"threshold": "100.00", "items": 2}, ...], lowest first. Each
    month, a developer whose usage the month before (purchases minus refunds; deposits and
    manual adjustments don't count) reached a level's threshold earns the items of the
    highest such level. The items are one total per developer, spent on any preferential
    good (one item per free unit; on a court, per slot) and saved up when unused. Checkout
    takes them automatically (see preferential.py). Every change is kept in
    `PreferentialRule`.
    """

    enabled = models.BooleanField(default=False)
    levels = models.JSONField(default=list)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        verbose_name_plural = "preferential settings"

    def __str__(self):
        return f"Preferential {'on' if self.enabled else 'off'}: {self.levels}"

    @classmethod
    def current(cls) -> "PreferentialSettings":
        return cls.objects.get_or_create(pk=1)[0]


class PreferentialGood(TimeStampedModel):
    """A good (or court) that free preferential items can pay for."""

    good = models.OneToOneField(Good, on_delete=models.CASCADE, related_name="preferential")

    class Meta:
        ordering = ["good__name", "id"]

    def __str__(self):
        return f"Preferential: {self.good}"


class PreferentialRule(models.Model):
    """The preferential settings as saved at `created_at` (never changed). A month's free
    items follow the settings in force at the end of that month (now, for this month), so
    later changes never alter what was already earned."""

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    enabled = models.BooleanField()
    # [{"threshold": "100.00", "items": 2}, ...], lowest first (see PreferentialSettings).
    levels = models.JSONField(default=list)
    # The preferential goods' ids then, for the record.
    goods = models.JSONField(default=list)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"Preferential rule {self.created_at:%Y-%m-%d %H:%M}"
