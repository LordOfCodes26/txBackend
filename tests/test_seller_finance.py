import threading
import uuid
from decimal import Decimal

import pytest
from django.db import IntegrityError, connection, transaction

from apps.accounts.rbac import Roles
from apps.audit.models import AuditLog
from apps.seller_finance import services
from apps.seller_finance.exceptions import InsufficientSellerBalance
from apps.seller_finance.models import SellerAccount, SellerPayment, SellerTransaction
from apps.sellers.models import Seller

ACCOUNTS = "/api/v1/seller-finance/accounts/"
TXNS = "/api/v1/seller-finance/transactions/"
PAYOUTS = "/api/v1/seller-finance/payouts/"
ADJUST = "/api/v1/seller-finance/adjustments/"


def key():
    return str(uuid.uuid4())


@pytest.fixture
def seller_user(make_user):
    return make_user(Roles.SELLER, email="cafe@x.com")


@pytest.fixture
def seller(db, seller_user):
    """A seller who has earned 100.00 from two sales."""
    seller = Seller.objects.create(name="Cafe", user=seller_user)
    with transaction.atomic():
        services.credit_sale(seller=seller, amount="60.00", reference="purchase:1")
        services.credit_sale(seller=seller, amount="40.00", reference="purchase:2")
    return seller


def client_for(user):
    """A separate client per user (the shared `auth_client` re-authenticates one client)."""
    from rest_framework.test import APIClient

    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def finance(make_user):
    return client_for(make_user(Roles.FINANCE_MANAGER))


@pytest.fixture
def finance2(make_user):
    return client_for(make_user(Roles.FINANCE_MANAGER))


def request(client, amount, seller=None, idem=None):
    data = {"amount": amount} | ({"seller": seller.pk} if seller else {})
    return client.post(PAYOUTS, data, HTTP_IDEMPOTENCY_KEY=idem or key())


def account(seller):
    return services.with_reserved(SellerAccount.objects.filter(seller=seller)).get()


# --- Balance and ledger --------------------------------------------------------------


@pytest.mark.django_db
def test_sales_credit_the_seller(auth_client, seller_user, seller):
    body = client_for(seller_user).get(f"{ACCOUNTS}me/").json()
    assert (body["balance"], body["reserved"], body["available_balance"]) == (
        "100.00",
        "0.00",
        "100.00",
    )
    txns = client_for(seller_user).get(TXNS).json()["results"]
    assert [t["amount"] for t in txns] == ["40.00", "60.00"]


@pytest.mark.django_db
def test_same_purchase_cannot_be_credited_twice(seller):
    with pytest.raises(IntegrityError), transaction.atomic():
        services.credit_sale(seller=seller, amount="60.00", reference="purchase:1")


@pytest.mark.django_db
def test_seller_ledger_is_append_only(seller):
    with pytest.raises(IntegrityError), transaction.atomic():
        SellerTransaction.objects.all().update(amount=1)


# --- Payout lifecycle ------------------------------------------------------------------


@pytest.mark.django_db
def test_full_payout_lifecycle(auth_client, seller_user, seller, finance):
    seller_client = client_for(seller_user)
    r = request(seller_client, "70.00")
    assert r.status_code == 201, r.json()
    pid = r.json()["id"]
    assert account(seller).reserved == Decimal("70.00")

    assert finance.post(f"{PAYOUTS}{pid}/approve/").json()["status"] == "APPROVED"
    assert finance.post(f"{PAYOUTS}{pid}/processing/").json()["status"] == "PROCESSING"
    body = finance.post(f"{PAYOUTS}{pid}/pay/", {"payment_reference": "TRX-8841"}).json()
    assert (body["status"], body["payment_reference"]) == ("PAID", "TRX-8841")

    acc = account(seller)
    assert (acc.balance, acc.reserved) == (Decimal("30.00"), Decimal("0.00"))
    payout_txn = SellerTransaction.objects.get(kind="PAYOUT")
    assert (payout_txn.amount, payout_txn.reference) == (Decimal("-70.00"), f"payout:{pid}")
    actions = set(AuditLog.objects.values_list("action", flat=True))
    assert {"seller.payout_requested", "seller.payout_approved", "seller.payout_paid"} <= actions


@pytest.mark.django_db
def test_open_payouts_reserve_balance(auth_client, seller_user, seller):
    client = client_for(seller_user)
    assert request(client, "70.00").status_code == 201
    r = request(client, "40.00")
    assert r.status_code == 409
    assert r.json()["error"] == {
        "code": "INSUFFICIENT_SELLER_BALANCE",
        "message": "The seller's available balance is too low.",
        "details": {"available": "30.00"},
    }
    assert request(client, "30.00").status_code == 201


@pytest.mark.django_db
def test_rejected_and_cancelled_payouts_release_reservation(
    auth_client, seller_user, seller, finance
):
    client = client_for(seller_user)
    a = request(client, "60.00").json()["id"]
    b = request(client, "40.00").json()["id"]

    r = finance.post(f"{PAYOUTS}{a}/reject/", {"reason": "Bank details missing"})
    assert (r.json()["status"], r.json()["rejection_reason"]) == (
        "REJECTED",
        "Bank details missing",
    )
    assert client_for(seller_user).post(f"{PAYOUTS}{b}/cancel/").json()["status"] == "CANCELLED"
    assert account(seller).reserved == 0
    assert account(seller).balance == Decimal("100.00")


@pytest.mark.django_db
def test_requester_cannot_approve_own_request(auth_client, make_user, seller, finance, finance2):
    pid = request(finance, "10.00", seller=seller).json()["id"]
    r = finance.post(f"{PAYOUTS}{pid}/approve/")
    assert (r.status_code, r.json()["error"]["code"]) == (403, "SELF_APPROVAL_FORBIDDEN")
    assert finance2.post(f"{PAYOUTS}{pid}/approve/").json()["status"] == "APPROVED"


@pytest.mark.django_db
def test_seller_linked_finance_user_cannot_pay_own_seller(
    auth_client, make_user, seller, seller_user, finance
):
    # A finance manager who is also the seller's login must not approve/pay themselves.
    from apps.accounts.models import Role, UserRole

    UserRole.objects.create(user=seller_user, role=Role.objects.get(code=Roles.FINANCE_MANAGER))
    pid = request(finance, "10.00", seller=seller).json()["id"]
    r = client_for(seller_user).post(f"{PAYOUTS}{pid}/approve/")
    assert r.json()["error"]["code"] == "OWN_SELLER_FORBIDDEN"


@pytest.mark.django_db
def test_invalid_transitions(auth_client, seller_user, seller, finance, finance2):
    pid = request(client_for(seller_user), "10.00").json()["id"]
    r = finance.post(f"{PAYOUTS}{pid}/pay/", {"payment_reference": "X"})
    assert r.json()["error"]["code"] == "INVALID_PAYOUT_TRANSITION"
    finance.post(f"{PAYOUTS}{pid}/approve/")
    assert finance.post(f"{PAYOUTS}{pid}/pay/", {}).status_code == 400  # reference required
    finance.post(f"{PAYOUTS}{pid}/pay/", {"payment_reference": "CASH-1"})
    for act in ("approve", "processing", "cancel"):
        assert finance.post(f"{PAYOUTS}{pid}/{act}/").status_code == 409, act
    r = finance.post(f"{PAYOUTS}{pid}/reject/", {"reason": "x"})
    assert r.json()["error"]["code"] == "INVALID_PAYOUT_TRANSITION"


@pytest.mark.django_db
def test_payout_request_is_idempotent(auth_client, seller_user, seller):
    client = client_for(seller_user)
    idem = key()
    first, again = request(client, "10.00", idem=idem), request(client, "10.00", idem=idem)
    assert (first.status_code, again.status_code) == (201, 200)
    assert SellerPayment.objects.count() == 1
    assert request(client, "11.00", idem=idem).json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


# --- Access and scoping ----------------------------------------------------------------


@pytest.mark.django_db
def test_seller_sees_and_requests_only_for_self(auth_client, make_user, seller_user, seller):
    other_user = make_user(Roles.SELLER)
    other = Seller.objects.create(name="Other", user=other_user)
    with transaction.atomic():
        services.credit_sale(seller=other, amount="5.00", reference="purchase:3")
    client = client_for(seller_user)

    assert request(client, "1.00", seller=other).status_code == 400
    mine = request(client, "1.00").json()
    assert mine["seller"]["id"] == seller.pk
    assert {t["account"] for t in client.get(TXNS).json()["results"]} == {seller.account.pk}
    assert client.get(ACCOUNTS).status_code == 403  # all balances need seller_finance.view

    other_client = client_for(other_user)
    assert other_client.get(f"{PAYOUTS}{mine['id']}/").status_code == 404
    assert other_client.post(f"{PAYOUTS}{mine['id']}/cancel/").status_code == 404


@pytest.mark.django_db
def test_sellers_cannot_approve_or_pay(auth_client, seller_user, seller):
    client = client_for(seller_user)
    pid = request(client, "10.00").json()["id"]
    assert client.post(f"{PAYOUTS}{pid}/approve/").status_code == 403
    assert client.post(f"{PAYOUTS}{pid}/pay/", {"payment_reference": "x"}).status_code == 403


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("role", "view", "payout"),
    [
        (Roles.BOSS, 200, 201),
        (Roles.FINANCE_MANAGER, 200, 201),
        (Roles.SELLER_MANAGER, 200, 403),
        (Roles.MANAGER, 403, 403),
        (Roles.DEVELOPER, 403, 403),
    ],
)
def test_access_by_role(auth_client, make_user, seller, role, view, payout):
    client = auth_client(make_user(role))
    assert client.get(ACCOUNTS).status_code == view
    assert request(client, "1.00", seller=seller).status_code == payout


# --- Adjustments -------------------------------------------------------------------------


@pytest.mark.django_db
def test_adjustment_respects_reserved_money(auth_client, seller_user, seller, finance):
    request(client_for(seller_user), "80.00")

    def adjust(amount):
        return finance.post(
            ADJUST,
            {"seller": seller.pk, "amount": amount, "reason": "Correction"},
            HTTP_IDEMPOTENCY_KEY=key(),
        )

    r = adjust("-25.00")
    assert (r.status_code, r.json()["error"]["details"]) == (409, {"available": "20.00"})
    assert adjust("-20.00").json()["balance_after"] == "80.00"
    assert adjust("5.00").json()["balance_after"] == "85.00"
    assert AuditLog.objects.filter(action="seller.balance_adjusted").count() == 2


# --- Reconciliation ------------------------------------------------------------------------


@pytest.mark.django_db
def test_reconciliation_detects_uncredited_purchase(seller):
    # The fixture credits 100.00 with no matching purchases, so sales != purchases.
    [problem] = services.seller_ledger_mismatches()
    assert (problem["credited"], problem["purchases"]) == ("100.00", "0.00")


# --- Concurrency ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_concurrent_payout_requests_cannot_exceed_balance(make_user):
    user = make_user(Roles.SELLER)
    seller = Seller.objects.create(name="Cafe", user=user)
    with transaction.atomic():
        services.credit_sale(seller=seller, amount="100.00", reference="purchase:1")

    barrier = threading.Barrier(5)
    outcomes = []

    def ask(i):
        try:
            barrier.wait()
            outcomes.append(
                services.request_payout(
                    actor=user, seller=seller, amount="30.00", idempotency_key=f"conc-pay-{i:04d}"
                )
            )
        except InsufficientSellerBalance as exc:
            outcomes.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=ask, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(isinstance(o, tuple) for o in outcomes) == 3  # 3 × 30 ≤ 100 < 4 × 30
    assert services.reserved_amount(seller.pk) == Decimal("90.00")
