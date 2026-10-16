from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.developers.models import Developer
from common.models import AppendOnlyModel, TimeStampedModel

MONEY = {"max_digits": 14, "decimal_places": 2}
# Purchase PINs are exactly this many digits.
PIN_LENGTH = 4


class AccountStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    FROZEN = "FROZEN", "Frozen (can receive money, cannot spend)"
    CLOSED = "CLOSED", "Closed"


class DeveloperAccount(TimeStampedModel):
    """A developer's prepaid balance.

    `balance` is a cache of the ledger (`AccountTransaction`). It only changes through
    `finance.services.post_transaction`, which locks this row, so concurrent deposits
    and purchases are applied one at a time. Purchases and bookings may take it below
    zero down to the company's minus limit (`WalletSettings`); nothing else can.
    """

    developer = models.OneToOneField(Developer, on_delete=models.PROTECT, related_name="account")
    balance = models.DecimalField(**MONEY, default=0)
    status = models.CharField(
        max_length=10, choices=AccountStatus.choices, default=AccountStatus.ACTIVE
    )
    status_reason = models.CharField(max_length=255, blank=True)
    # Purchase PIN, entered by the developer at the till. Only a hash is stored.
    pin_hash = models.CharField(max_length=128, blank=True)
    pin_failed_attempts = models.PositiveSmallIntegerField(default=0)
    pin_locked_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["developer__full_name", "id"]

    def __str__(self):
        return f"{self.developer} ({self.balance})"

    @property
    def has_pin(self) -> bool:
        return bool(self.pin_hash)


class TransactionKind(models.TextChoices):
    DEPOSIT = "DEPOSIT", "Deposit"
    PURCHASE = "PURCHASE", "Purchase"
    REFUND = "REFUND", "Refund"
    ADJUSTMENT = "ADJUSTMENT", "Manual adjustment"


class AccountTransaction(AppendOnlyModel):
    """One ledger entry. Immutable (DB trigger); mistakes are fixed with new entries.

    `amount` is signed: credits are positive, debits negative.
    """

    account = models.ForeignKey(
        DeveloperAccount, on_delete=models.PROTECT, related_name="transactions"
    )
    kind = models.CharField(max_length=12, choices=TransactionKind.choices)
    amount = models.DecimalField(**MONEY)
    balance_after = models.DecimalField(**MONEY)
    description = models.CharField(max_length=255, blank=True)
    reference = models.CharField(max_length=100, blank=True, help_text="e.g. purchase id.")
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
                    Q(kind=TransactionKind.DEPOSIT, amount__gt=0)
                    | Q(kind=TransactionKind.REFUND, amount__gt=0)
                    | Q(kind=TransactionKind.PURCHASE, amount__lt=0)
                    | (Q(kind=TransactionKind.ADJUSTMENT) & ~Q(amount=0))
                ),
                name="transaction_amount_sign_matches_kind",
            ),
            models.UniqueConstraint(
                "idempotency_key",
                condition=~Q(idempotency_key=""),
                name="transaction_idempotency_key_unique",
            ),
        ]
        indexes = [models.Index(fields=["account", "created_at"])]

    def __str__(self):
        return f"{self.kind} {self.amount} → {self.balance_after}"


class WalletSettings(TimeStampedModel):
    """Company-wide wallet rules, set by admins. One row (pk=1); see `current()`."""

    minus_limit = models.DecimalField(
        **MONEY,
        default=0,
        validators=[MinValueValidator(0)],
        help_text="How far below zero purchases and bookings may take a wallet (0: never).",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        verbose_name_plural = "wallet settings"
        constraints = [
            models.CheckConstraint(
                condition=Q(minus_limit__gte=0), name="wallet_minus_limit_not_negative"
            ),
        ]

    def __str__(self):
        return f"Minus limit {self.minus_limit}"

    @classmethod
    def current(cls) -> "WalletSettings":
        return cls.objects.get_or_create(pk=1)[0]
