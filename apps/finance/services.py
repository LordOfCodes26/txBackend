from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import IntegrityError, transaction
from django.db.models import OuterRef, Subquery, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.audit.services import record_audit
from apps.developers.models import Developer

from .exceptions import (
    AccountNotActive,
    DepositLimitExceeded,
    IdempotencyKeyReused,
    InsufficientBalance,
    InvalidAccountTransition,
    InvalidPin,
    PinLocked,
    PinNotSet,
    SelfTransactionForbidden,
)
from .models import AccountStatus, AccountTransaction, DeveloperAccount, TransactionKind

CENT = Decimal("0.01")


def money(value) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def open_account(developer: Developer) -> DeveloperAccount:
    account, _created = DeveloperAccount.objects.get_or_create(developer=developer)
    return account


def post_transaction(
    *,
    account: DeveloperAccount,
    kind: str,
    amount: Decimal,
    actor=None,
    description: str = "",
    reference: str = "",
    idempotency_key: str = "",
) -> AccountTransaction:
    """The only way a balance changes. Must run inside a transaction.

    Locks the account row: concurrent deposits and purchases on one account are applied
    strictly one after another, and the balance can never go below zero. Checkout will
    call this with kind=PURCHASE after locking the account first.
    """
    amount = money(amount)
    account = DeveloperAccount.objects.select_for_update().get(pk=account.pk)
    if account.status == AccountStatus.CLOSED:
        raise AccountNotActive(_("This account is closed."))
    if amount < 0 and account.status == AccountStatus.FROZEN:
        raise AccountNotActive(_("This account is frozen and cannot be debited."))

    new_balance = account.balance + amount
    if new_balance < 0:
        raise InsufficientBalance(
            details={"balance": str(account.balance), "required": str(-amount)}
        )
    account.balance = new_balance
    account.save(update_fields=["balance", "updated_at"])
    return AccountTransaction.objects.create(
        account=account,
        kind=kind,
        amount=amount,
        balance_after=new_balance,
        description=description,
        reference=reference,
        idempotency_key=idempotency_key,
        actor=actor if getattr(actor, "pk", None) else None,
    )


def _ensure_not_own_account(actor, account: DeveloperAccount) -> None:
    user_id = account.developer.user_id
    if user_id is not None and user_id == getattr(actor, "pk", None):
        raise SelfTransactionForbidden()


def _previous(key: str, *, account, kind, amount) -> AccountTransaction | None:
    existing = AccountTransaction.objects.filter(idempotency_key=key).first()
    if existing is None:
        return None
    if (existing.account_id, existing.kind, existing.amount) != (account.pk, kind, amount):
        raise IdempotencyKeyReused()
    return existing


def _post_once(
    *, actor, account, kind, amount, description, idempotency_key, audit_action
) -> tuple[AccountTransaction, bool]:
    """Post a manual transaction exactly once per idempotency key. Returns (txn, created)."""
    existing = _previous(idempotency_key, account=account, kind=kind, amount=amount)
    if existing:
        return existing, False
    try:
        with transaction.atomic():
            txn = post_transaction(
                account=account,
                kind=kind,
                amount=amount,
                actor=actor,
                description=description,
                idempotency_key=idempotency_key,
            )
            record_audit(
                audit_action,
                actor=actor,
                entity=account,
                old_values={"balance": txn.balance_after - amount},
                new_values={
                    "balance": txn.balance_after,
                    "amount": amount,
                    "transaction": txn.pk,
                    "description": description,
                },
            )
    except IntegrityError:
        # A concurrent request with the same key committed first.
        existing = _previous(idempotency_key, account=account, kind=kind, amount=amount)
        if existing is None:
            raise
        return existing, False
    return txn, True


def deposit(
    *,
    actor,
    developer: Developer,
    amount,
    idempotency_key: str,
    description: str = "",
    pin: str | None = None,
) -> tuple[AccountTransaction, bool]:
    amount = money(amount)
    limit = money(settings.FINANCE_MAX_DEPOSIT)
    if amount > limit:
        raise DepositLimitExceeded(details={"max": str(limit)})
    account = open_account(developer)
    _ensure_not_own_account(actor, account)
    if pin is not None:
        verify_pin(account=account, pin=pin)
    return _post_once(
        actor=actor,
        account=account,
        kind=TransactionKind.DEPOSIT,
        amount=amount,
        description=description or "Deposit",
        idempotency_key=idempotency_key,
        audit_action="finance.deposit",
    )


def adjust(
    *, actor, developer: Developer, amount, reason: str, idempotency_key: str
) -> tuple[AccountTransaction, bool]:
    """Signed manual correction (e.g. reverse a mistaken deposit)."""
    account = open_account(developer)
    _ensure_not_own_account(actor, account)
    return _post_once(
        actor=actor,
        account=account,
        kind=TransactionKind.ADJUSTMENT,
        amount=money(amount),
        description=reason,
        idempotency_key=idempotency_key,
        audit_action="finance.adjustment",
    )


_TRANSITIONS = {
    "freeze": ({AccountStatus.ACTIVE}, AccountStatus.FROZEN, "finance.account_frozen"),
    "unfreeze": ({AccountStatus.FROZEN}, AccountStatus.ACTIVE, "finance.account_unfrozen"),
    "close": (
        {AccountStatus.ACTIVE, AccountStatus.FROZEN},
        AccountStatus.CLOSED,
        "finance.account_closed",
    ),
    "reopen": ({AccountStatus.CLOSED}, AccountStatus.ACTIVE, "finance.account_reopened"),
}


@transaction.atomic
def change_status(
    *, actor, account: DeveloperAccount, transition: str, reason: str = ""
) -> DeveloperAccount:
    allowed_from, target, action = _TRANSITIONS[transition]
    account = DeveloperAccount.objects.select_for_update().get(pk=account.pk)
    if account.status not in allowed_from:
        raise InvalidAccountTransition()
    if target == AccountStatus.CLOSED and account.balance != 0:
        raise InvalidAccountTransition(
            _("Pay out or adjust the remaining balance to zero before closing."),
            details={"balance": str(account.balance)},
        )
    old = account.status
    account.status = target
    account.status_reason = reason
    account.save(update_fields=["status", "status_reason", "updated_at"])
    record_audit(
        action,
        actor=actor,
        entity=account,
        old_values={"status": old},
        new_values={"status": target, "reason": reason},
    )
    return account


def ledger_mismatches() -> list[dict]:
    """Accounts whose balance differs from the ledger sum or from the last balance_after."""
    last = AccountTransaction.objects.filter(account=OuterRef("pk")).order_by("-created_at", "-id")
    rows = DeveloperAccount.objects.annotate(
        ledger=Sum("transactions__amount"),
        last_balance=Subquery(last.values("balance_after")[:1]),
    ).values("id", "balance", "ledger", "last_balance")
    problems = []
    for row in rows:
        ledger = row["ledger"] or Decimal("0")
        last_balance = row["last_balance"] if row["last_balance"] is not None else Decimal("0")
        if not (row["balance"] == ledger == last_balance):
            problems.append(
                {k: str(v) if isinstance(v, Decimal) else v for k, v in row.items()}
                | {"ledger": str(ledger)}
            )
    return problems


# --- Purchase PIN ---------------------------------------------------------------


def _pin_is_trivial(pin: str) -> bool:
    digits = [int(c) for c in pin]
    steps = {b - a for a, b in zip(digits, digits[1:], strict=False)}
    return len(set(digits)) == 1 or steps in ({1}, {-1})


def validate_pin_format(pin: str) -> None:
    from rest_framework.exceptions import ValidationError

    if not (pin.isdigit() and 4 <= len(pin) <= 6):
        raise ValidationError({"pin": [_("The PIN must be 4 to 6 digits.")]})
    if _pin_is_trivial(pin):
        raise ValidationError({"pin": [_("Choose a less obvious PIN (not 1111 or 1234).")]})


@transaction.atomic
def set_pin(*, actor, account: DeveloperAccount, pin: str, current_pin: str | None) -> None:
    """The developer sets or changes their own PIN (changing requires the current one)."""
    account = DeveloperAccount.objects.select_for_update().get(pk=account.pk)
    if account.pin_hash and not (current_pin and check_password(current_pin, account.pin_hash)):
        from rest_framework.exceptions import ValidationError

        raise ValidationError({"current_pin": [_("The current PIN is incorrect.")]})
    validate_pin_format(pin)
    changed = bool(account.pin_hash)
    account.pin_hash = make_password(pin)
    account.pin_failed_attempts = 0
    account.pin_locked_until = None
    account.save(
        update_fields=["pin_hash", "pin_failed_attempts", "pin_locked_until", "updated_at"]
    )
    record_audit(
        "finance.pin_changed" if changed else "finance.pin_set", actor=actor, entity=account
    )


@transaction.atomic
def give_pin(*, actor, account: DeveloperAccount, pin: str, action: str) -> None:
    """Staff store a PIN the developer typed in front of them (when a card is assigned,
    or to replace a forgotten one). Clears any lockout. The PIN itself is never logged."""
    validate_pin_format(pin)
    account = DeveloperAccount.objects.select_for_update().get(pk=account.pk)
    account.pin_hash = make_password(pin)
    account.pin_failed_attempts = 0
    account.pin_locked_until = None
    account.save(
        update_fields=["pin_hash", "pin_failed_attempts", "pin_locked_until", "updated_at"]
    )
    record_audit(action, actor=actor, entity=account)


def change_pin_at_desk(*, actor, account: DeveloperAccount, current_pin: str, pin: str) -> None:
    """The developer changes their PIN at the PIN desk (identified by their card).

    The current PIN is checked like at the till: wrong attempts count and lock the PIN
    (verify_pin commits them). Only then is the new PIN stored."""
    verify_pin(account=account, pin=current_pin)
    give_pin(actor=actor, account=account, pin=pin, action="finance.pin_changed_at_desk")


@transaction.atomic
def reset_pin(*, actor, account: DeveloperAccount, pin: str | None = None) -> None:
    """Replace a forgotten PIN with `pin` (typed by the developer), or clear it. Clears
    any lockout."""
    if pin:
        give_pin(actor=actor, account=account, pin=pin, action="finance.pin_reset")
        return
    account = DeveloperAccount.objects.select_for_update().get(pk=account.pk)
    account.pin_hash = ""
    account.pin_failed_attempts = 0
    account.pin_locked_until = None
    account.save(
        update_fields=["pin_hash", "pin_failed_attempts", "pin_locked_until", "updated_at"]
    )
    record_audit("finance.pin_reset", actor=actor, entity=account)


def verify_pin(*, account: DeveloperAccount, pin: str) -> None:
    """Check a PIN entered at the till.

    Runs in its OWN transaction and commits even when the PIN is wrong, so failed
    attempts are counted although the purchase that triggered them is rejected. After
    PURCHASE_PIN_MAX_ATTEMPTS failures the PIN is locked for PURCHASE_PIN_LOCKOUT_MINUTES.
    """
    failure = None
    with transaction.atomic():
        account = DeveloperAccount.objects.select_for_update().get(pk=account.pk)
        now = timezone.now()
        if account.pin_locked_until and account.pin_locked_until > now:
            raise PinLocked(details={"locked_until": account.pin_locked_until.isoformat()})
        if not account.pin_hash:
            raise PinNotSet()
        if check_password(pin, account.pin_hash):
            if account.pin_failed_attempts or account.pin_locked_until:
                account.pin_failed_attempts = 0
                account.pin_locked_until = None
                account.save(update_fields=["pin_failed_attempts", "pin_locked_until"])
            return

        account.pin_failed_attempts += 1
        remaining = settings.PURCHASE_PIN_MAX_ATTEMPTS - account.pin_failed_attempts
        if remaining <= 0:
            account.pin_failed_attempts = 0
            account.pin_locked_until = now + timedelta(
                minutes=settings.PURCHASE_PIN_LOCKOUT_MINUTES
            )
            record_audit("finance.pin_locked", entity=account)
            failure = PinLocked(details={"locked_until": account.pin_locked_until.isoformat()})
        else:
            failure = InvalidPin(details={"attempts_remaining": remaining})
        account.save(update_fields=["pin_failed_attempts", "pin_locked_until"])
    raise failure
