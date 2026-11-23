import io
from datetime import timedelta

import pytest
from django.core.management import CommandError, call_command
from django.db.models import Count
from django.db.models.functions import Lower
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.rbac import Roles
from apps.audit.models import AuditLog
from apps.cash import reports
from apps.cash import services as cash
from apps.cash.models import CashEntry, StockCount
from apps.goods import services as goods
from apps.goods.models import Good
from apps.purchases.models import Purchase
from apps.rfid.models import RFIDCard
from apps.sellers.models import Seller, ServicePosition


@pytest.mark.django_db
def test_demo_seeds_run_one_after_the_other_with_unique_card_labels(settings):
    """The offline kit's --seed-demo runs these on a development copy. Card labels are
    unique, so seed_more must not reuse the labels of seed_demo's spare cards."""
    settings.DEBUG = True
    call_command("seed_demo", stdout=io.StringIO())
    call_command("seed_more", "--developers", "10", stdout=io.StringIO())
    clashes = (
        RFIDCard.objects.exclude(label="")
        .values(key=Lower("label"))
        .annotate(n=Count("pk"))
        .filter(n__gt=1)
    )
    assert list(clashes) == []
    assert RFIDCard.objects.filter(label="0031").exists()  # seed_more's own card
    assert RFIDCard.objects.filter(label="S031").exists()  # a seed_demo spare


@pytest.fixture
def cash_shop(db, make_user, settings):
    settings.TEST_CONSOLE_ENABLED = True
    make_user(Roles.ADMIN, username="root")
    make_user(Roles.FINANCE_MANAGER, username="fin")
    seller = Seller.objects.create(
        name="Demo Kiosk", user=make_user(Roles.GOODS_SELLER, username="kiosk")
    )
    position = ServicePosition.objects.create(seller=seller, name="Front")
    for n in range(6):
        goods.create_good(
            actor=None,
            service_position=position,
            name=f"Good {n}",
            price=f"{n + 1}.50",
            initial_quantity=3,
            decimal_quantity=n == 5,
        )
    return seller


def _stock():
    return dict(Good.objects.filter(track_stock=True).values_list("name", "quantity"))


def _prices():
    return dict(Good.objects.values_list("name", "price"))


def test_seed_cash_and_undo(cash_shop):
    stock, prices = _stock(), _prices()
    call_command("seed_cash", "--days", "10", "--per-day", "5", stdout=io.StringIO())
    cash_shop.refresh_from_db()
    assert cash_shop.accepts_cash and _stock() == stock  # books are checked by the command
    assert Purchase.objects.filter(payment="CASH", status="CONFIRMED").count() > 10
    assert Purchase.objects.filter(payment="CASH", status="REFUNDED").count() == 2
    assert set(CashEntry.objects.values_list("kind", flat=True)) == {
        "SALE",
        "RETURN",
        "HANDOVER",
        "MISSING",
    }
    assert CashEntry.objects.filter(reverses__isnull=False).count() == 1
    today = timezone.localdate()
    changes = reports.price_changes(Seller.objects.all(), today - timedelta(days=10), today)
    seeded = AuditLog.objects.filter(request_id="seed-cash").values_list("pk", flat=True)
    assert len(seeded) == 4 and set(seeded) <= {c["id"] for c in changes}
    assert set(StockCount.objects.values_list("outcome", flat=True)) == {
        "EXPLAINED",
        "CHARGED",
        "OPEN",
    }
    now = timezone.now()
    assert not CashEntry.objects.filter(created_at__gt=now).exists()
    moments = list(CashEntry.objects.order_by("id").values_list("created_at", flat=True))
    assert moments == sorted(moments)
    with pytest.raises(CommandError):  # once per store
        call_command("seed_cash", stdout=io.StringIO())
    call_command("seed_cash", "--undo", stdout=io.StringIO())
    cash_shop.refresh_from_db()
    assert not cash_shop.accepts_cash and _stock() == stock and _prices() == prices
    for model in (CashEntry, StockCount, Purchase):
        assert not model.objects.exists()
    assert not AuditLog.objects.filter(request_id="seed-cash").exists()
    call_command("seed_cash", "--days", "7", "--per-day", "2", stdout=io.StringIO())  # and again


def test_undoing_seeded_cash_after_people_used_it(cash_shop):
    stock, prices = _stock(), _prices()
    call_command("seed_cash", "--days", "8", "--per-day", "3", stdout=io.StringIO())
    fin = User.objects.get(username="fin")
    sale = Purchase.objects.filter(status="CONFIRMED").first()
    cash.delete_sale(actor=fin, purchase=sale, reason="x")
    count = StockCount.objects.get(outcome="OPEN")
    cash.resolve_count(actor=fin, count=count, outcome="CHARGED", note="x")
    handed = CashEntry.objects.filter(kind="HANDOVER", reversal=None, reverses=None).first()
    cash.reverse_entry(actor=fin, entry=handed, reason="x", idempotency_key="real-0001")
    cash.hand_over(actor=fin, seller=cash_shop, amount="1.00", idempotency_key="real-0002")
    call_command("seed_cash", "--undo", stdout=io.StringIO())
    assert _stock() == stock and _prices() == prices
    real = CashEntry.objects.get()  # the real handover stays, so cash stays on
    assert (real.idempotency_key, real.owed_after) == ("real-0002", -1)
    cash_shop.refresh_from_db()
    assert cash_shop.accepts_cash


def test_seed_cash_is_for_test_servers(cash_shop, settings):
    settings.TEST_CONSOLE_ENABLED = False
    settings.DEBUG = False
    with pytest.raises(CommandError):
        call_command("seed_cash", stdout=io.StringIO())
