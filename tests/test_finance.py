import threading
import uuid
from decimal import Decimal

import pytest
from django.db import IntegrityError, connection, transaction

from apps.accounts.rbac import Roles
from apps.audit.models import AuditLog
from apps.developers.models import Developer
from apps.finance import services
from apps.finance.exceptions import InsufficientBalance
from apps.finance.models import AccountTransaction, DeveloperAccount

DEPOSITS = "/api/v1/finance/deposits/"
ADJUSTMENTS = "/api/v1/finance/adjustments/"
ACCOUNTS = "/api/v1/finance/accounts/"
TRANSACTIONS = "/api/v1/finance/transactions/"


def key():
    return str(uuid.uuid4())


@pytest.fixture
def developer(db):
    dev = Developer.objects.create(employee_number="E1", full_name="Ada")
    services.open_account(dev)
    return dev


@pytest.fixture
def finance(auth_client, make_user):
    return auth_client(make_user(Roles.FINANCE_MANAGER))


def deposit(client, developer, amount, idem=None, **extra):
    return client.post(
        DEPOSITS,
        {"developer": developer.pk, "amount": amount, **extra},
        HTTP_IDEMPOTENCY_KEY=idem or key(),
    )


def balance(developer):
    return DeveloperAccount.objects.get(developer=developer).balance


# --- Deposits -----------------------------------------------------------------


@pytest.mark.django_db
def test_deposit_updates_balance_ledger_and_audit(finance, developer):
    response = deposit(finance, developer, "500.00", description="Monthly allowance")
    assert response.status_code == 201, response.json()
    body = response.json()
    assert (body["kind"], body["amount"], body["balance_after"]) == ("DEPOSIT", "500.00", "500.00")
    assert balance(developer) == Decimal("500.00")

    log = AuditLog.objects.get(action="finance.deposit")
    assert log.new_values["balance"] == "500.00" and log.old_values == {"balance": "0.00"}
    assert services.ledger_mismatches() == []


@pytest.mark.django_db
def test_same_idempotency_key_deposits_once(finance, developer):
    idem = key()
    first = deposit(finance, developer, "50.00", idem)
    again = deposit(finance, developer, "50.00", idem)
    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json()["id"] == again.json()["id"]
    assert balance(developer) == Decimal("50.00")


@pytest.mark.django_db
def test_idempotency_key_reuse_with_different_amount_conflicts(finance, developer):
    idem = key()
    deposit(finance, developer, "50.00", idem)
    response = deposit(finance, developer, "60.00", idem)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.django_db
def test_deposit_requires_idempotency_key(finance, developer):
    response = finance.post(DEPOSITS, {"developer": developer.pk, "amount": "10"})
    assert response.status_code == 400
    assert "Idempotency-Key" in response.json()["error"]["details"]


@pytest.mark.django_db
@pytest.mark.parametrize("amount", ["0", "-5", "0.001", "abc"])
def test_deposit_amount_validation(finance, developer, amount):
    assert deposit(finance, developer, amount).status_code == 400


@pytest.mark.django_db
def test_deposit_limit(finance, developer, settings):
    settings.FINANCE_MAX_DEPOSIT = "100.00"
    response = deposit(finance, developer, "100.01")
    assert response.json()["error"]["code"] == "DEPOSIT_LIMIT_EXCEEDED"
    assert deposit(finance, developer, "100.00").status_code == 201


@pytest.mark.django_db
def test_cannot_deposit_to_own_account(auth_client, make_user, developer):
    user = make_user(Roles.FINANCE_MANAGER)
    developer.user = user
    developer.save()
    response = deposit(auth_client(user), developer, "10.00")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "SELF_TRANSACTION_FORBIDDEN"
    assert balance(developer) == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("role", "view", "deposit_status", "adjust_status"),
    [
        (Roles.ADMIN, 200, 201, 201),
        (Roles.FINANCE_MANAGER, 200, 201, 201),
        (Roles.MANAGER, 403, 403, 403),
        (Roles.DEVELOPER, 403, 403, 403),
        (Roles.SELLER, 403, 403, 403),
    ],
)
def test_finance_access_by_role(
    auth_client, make_user, developer, role, view, deposit_status, adjust_status
):
    client = auth_client(make_user(role))
    assert client.get(ACCOUNTS).status_code == view
    assert deposit(client, developer, "10.00").status_code == deposit_status
    response = client.post(
        ADJUSTMENTS,
        {"developer": developer.pk, "amount": "1.00", "reason": "x"},
        HTTP_IDEMPOTENCY_KEY=key(),
    )
    assert response.status_code == adjust_status


# --- Adjustments and status -----------------------------------------------------


@pytest.mark.django_db
def test_negative_adjustment_cannot_overdraw(finance, developer):
    deposit(finance, developer, "20.00")

    def adjust(amount):
        return finance.post(
            ADJUSTMENTS,
            {"developer": developer.pk, "amount": amount, "reason": "Reverse mistaken deposit"},
            HTTP_IDEMPOTENCY_KEY=key(),
        )

    response = adjust("-25.00")
    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "INSUFFICIENT_BALANCE",
        "message": "Developer account has insufficient balance.",
        "details": {"balance": "20.00", "required": "25.00"},
    }
    assert adjust("-20.00").json()["balance_after"] == "0.00"
    assert adjust("0").status_code == 400
    assert AuditLog.objects.filter(action="finance.adjustment").count() == 1


@pytest.mark.django_db
def test_adjustment_requires_reason(finance, developer):
    response = finance.post(
        ADJUSTMENTS, {"developer": developer.pk, "amount": "5"}, HTTP_IDEMPOTENCY_KEY=key()
    )
    assert "reason" in response.json()["error"]["details"]


@pytest.mark.django_db
def test_frozen_account_receives_but_cannot_spend(finance, developer):
    account = developer.account
    deposit(finance, developer, "30.00")
    assert (
        finance.post(f"{ACCOUNTS}{account.pk}/freeze/", {"reason": "Audit"}).json()["status"]
        == "FROZEN"
    )

    assert deposit(finance, developer, "5.00").status_code == 201
    with pytest.raises(Exception) as exc, transaction.atomic():
        services.post_transaction(account=account, kind="PURCHASE", amount=Decimal("-1"))
    assert exc.value.code == "ACCOUNT_NOT_ACTIVE"

    assert finance.post(f"{ACCOUNTS}{account.pk}/unfreeze/").json()["status"] == "ACTIVE"


@pytest.mark.django_db
def test_close_requires_zero_balance(finance, developer):
    account = developer.account
    deposit(finance, developer, "5.00")
    response = finance.post(f"{ACCOUNTS}{account.pk}/close/")
    assert response.status_code == 409
    assert response.json()["error"]["details"] == {"balance": "5.00"}

    finance.post(
        ADJUSTMENTS,
        {"developer": developer.pk, "amount": "-5.00", "reason": "Cash paid out"},
        HTTP_IDEMPOTENCY_KEY=key(),
    )
    assert finance.post(f"{ACCOUNTS}{account.pk}/close/").json()["status"] == "CLOSED"
    assert deposit(finance, developer, "1.00").json()["error"]["code"] == "ACCOUNT_NOT_ACTIVE"
    assert finance.post(f"{ACCOUNTS}{account.pk}/reopen/").json()["status"] == "ACTIVE"
    assert AuditLog.objects.filter(action="finance.account_closed").exists()


# --- Ledger integrity ------------------------------------------------------------


@pytest.mark.django_db
def test_ledger_is_append_only(finance, developer):
    deposit(finance, developer, "10.00")
    with pytest.raises(IntegrityError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("UPDATE finance_accounttransaction SET amount = 999")
    with pytest.raises(IntegrityError), transaction.atomic():
        AccountTransaction.objects.all().delete()


@pytest.mark.django_db
def test_database_rejects_negative_balance_and_wrong_signs(developer):
    with pytest.raises(IntegrityError), transaction.atomic():
        DeveloperAccount.objects.filter(developer=developer).update(balance=-1)
    with pytest.raises(IntegrityError), transaction.atomic():
        AccountTransaction.objects.create(
            account=developer.account, kind="DEPOSIT", amount=-5, balance_after=0
        )


@pytest.mark.django_db
def test_ledger_check_detects_drift(finance, developer):
    deposit(finance, developer, "10.00")
    assert services.ledger_mismatches() == []
    DeveloperAccount.objects.filter(developer=developer).update(balance=15)
    [problem] = services.ledger_mismatches()
    assert (problem["balance"], problem["ledger"]) == ("15.00", "10.00")


# --- Reading -----------------------------------------------------------------------


@pytest.mark.django_db
def test_developer_sees_own_account_and_transactions(auth_client, make_user, finance, developer):
    other = Developer.objects.create(employee_number="E2", full_name="Bob")
    deposit(finance, developer, "12.00")
    deposit(finance, other, "99.00")
    user = make_user(Roles.DEVELOPER)
    developer.user = user
    developer.save()

    client = auth_client(user)
    assert client.get(f"{ACCOUNTS}me/").json()["balance"] == "12.00"
    mine = client.get(f"{TRANSACTIONS}me/").json()["results"]
    assert [t["amount"] for t in mine] == ["12.00"]
    assert client.get(TRANSACTIONS).status_code == 403


@pytest.mark.django_db
def test_account_filters(finance, developer):
    other = Developer.objects.create(employee_number="E2", full_name="Bob", department="Research")
    deposit(finance, developer, "12.00")
    deposit(finance, other, "99.00")

    def names(query):
        results = finance.get(f"{ACCOUNTS}?{query}").json()["results"]
        return [a["developer"]["full_name"] for a in results]

    assert names("balance_min=50") == ["Bob"]
    assert names("department=research") == ["Bob"]
    assert names("ordering=-balance") == ["Bob", "Ada"]


@pytest.mark.django_db
def test_new_developers_get_an_account(auth_client, make_user):
    client = auth_client(make_user(Roles.MANAGER))
    response = client.post(
        "/api/v1/developers/",
        {"employee_number": "E9", "full_name": "New", "email": "new@x.com"},
    )
    assert DeveloperAccount.objects.filter(developer_id=response.json()["id"]).exists()


# --- Concurrency -----------------------------------------------------------------


def run_concurrently(n, fn):
    barrier = threading.Barrier(n)
    outcomes = []

    def worker(i):
        try:
            barrier.wait()
            outcomes.append(fn(i))
        except Exception as exc:  # noqa: BLE001
            outcomes.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return outcomes


@pytest.mark.django_db(transaction=True)
def test_concurrent_deposits_all_counted():
    dev = Developer.objects.create(employee_number="E1", full_name="Ada")
    services.open_account(dev)
    outcomes = run_concurrently(
        10,
        lambda i: services.deposit(
            actor=None, developer=dev, amount="10.00", idempotency_key=f"conc-dep-{i:04d}"
        ),
    )
    assert all(isinstance(o, tuple) for o in outcomes), outcomes
    assert balance(dev) == Decimal("100.00")
    assert services.ledger_mismatches() == []


@pytest.mark.django_db(transaction=True)
def test_concurrent_debits_never_overdraw():
    dev = Developer.objects.create(employee_number="E1", full_name="Ada")
    services.deposit(actor=None, developer=dev, amount="30.00", idempotency_key="seed-000001")

    def spend(i):
        with transaction.atomic():
            return services.post_transaction(
                account=dev.account, kind="PURCHASE", amount=Decimal("-10.00")
            )

    outcomes = run_concurrently(8, spend)
    assert sum(isinstance(o, AccountTransaction) for o in outcomes) == 3
    assert sum(isinstance(o, InsufficientBalance) for o in outcomes) == 5
    assert balance(dev) == Decimal("0.00")
    assert services.ledger_mismatches() == []


@pytest.mark.django_db(transaction=True)
def test_concurrent_retries_with_same_key_deposit_once():
    dev = Developer.objects.create(employee_number="E1", full_name="Ada")
    services.open_account(dev)
    outcomes = run_concurrently(
        5,
        lambda i: services.deposit(
            actor=None, developer=dev, amount="10.00", idempotency_key="same-key-0001"
        ),
    )
    assert all(isinstance(o, tuple) for o in outcomes), outcomes
    assert len({txn.pk for txn, _ in outcomes}) == 1
    assert sum(created for _, created in outcomes) == 1
    assert balance(dev) == Decimal("10.00")
