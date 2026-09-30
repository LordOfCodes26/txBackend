from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import OuterRef, Subquery, Sum

from apps.audit.services import record_audit
from apps.developers.models import Developer

from .exceptions import (
    AccountNotActive,
    DepositLimitExceeded,
    IdempotencyKeyReused,
    InsufficientBalance,
    InvalidAccountTransition,
    SelfTransactionForbidden,
)
from .models import AccountStatus, AccountTransaction, DeveloperAccount, TransactionKind

CENT = Decimal("0.01")


def money(value) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def open_account(developer: Developer) -> DeveloperAccount:
    account, _ = DeveloperAccount.objects.get_or_create(developer=developer)
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
        raise AccountNotActive("This account is closed.")
    if amount < 0 and account.status == AccountStatus.FROZEN:
        raise AccountNotActive("This account is frozen and cannot be debited.")

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
    *, actor, developer: Developer, amount, idempotency_key: str, description: str = ""
) -> tuple[AccountTransaction, bool]:
    amount = money(amount)
    limit = money(settings.FINANCE_MAX_DEPOSIT)
    if amount > limit:
        raise DepositLimitExceeded(details={"max": str(limit)})
    account = open_account(developer)
    _ensure_not_own_account(actor, account)
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
            "Pay out or adjust the remaining balance to zero before closing.",
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
