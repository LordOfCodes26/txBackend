"""Preferential service: free items each month for developers whose usage the month
before (purchases minus refunds, not deposits) reached the threshold, spent on any
preferential good and saved up when unused."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounts.rbac import Roles
from apps.audit.models import AuditLog
from apps.finance.models import AccountTransaction
from apps.purchases import preferential
from apps.purchases.models import PreferentialRule, PreferentialSettings, PurchaseItem
from tests import test_purchases
from tests.test_purchases import (
    PURCHASES,
    assert_books_balanced,
    balance,
    confirm,
    lines,
    new_purchase,
    paid,
    tap_on_till,
)

manual_card_entry = test_purchases.manual_card_entry
till = test_purchases.till
world = test_purchases.world

SETTINGS = "/api/v1/preferential/settings/"


def last_month(days_before_this_month=3):
    """A moment last month, company time."""
    _last, this_month, _next = preferential.month_bounds()
    return this_month - timedelta(days=days_before_this_month)


def ledger(account, entries):
    """Ledger entries made back then: (kind, amount, when). The ledger is append-only, so
    they are written that way from the start. They must add up to 0 and come before the
    wallet's other entries, so the balance and the books stay in step."""
    assert sum(Decimal(amount) for _kind, amount, _when in entries) == 0
    running = Decimal("0.00")
    for kind, amount, when in entries:
        running += Decimal(amount)
        AccountTransaction.objects.create(
            account=account,
            kind=kind,
            amount=Decimal(amount),
            balance_after=running,
            created_at=when,
        )


def spent_last_month(account, amount, days_before_this_month=4):
    """`amount` paid at a till last month (or `days_before_this_month`), out of a deposit
    made then too."""
    when = last_month(days_before_this_month)
    ledger(account, [("DEPOSIT", amount, when), ("PURCHASE", f"-{amount}", when)])


def turn_on(*goods, threshold="100.00", items=2, levels=None):
    preferential.save_settings(
        actor=None,
        enabled=True,
        levels=levels or [{"threshold": threshold, "items": items}],
        goods=list(goods),
    )


@pytest.fixture
def big_spender(world):
    """Ada spent 120.00 last month (150.00 minus a 30.00 refund; her deposit and a manual
    adjustment don't count). Anyone who spent 100.00 gets 2 free items a month, for coffee
    or cake (not tea)."""
    ledger(
        world.account,
        [
            ("DEPOSIT", "200.00", last_month(5)),
            ("PURCHASE", "-150.00", last_month(4)),
            ("REFUND", "30.00", last_month(3)),
            ("ADJUSTMENT", "-80.00", last_month(2)),
        ],
    )
    turn_on(world.coffee, world.cake)
    return world


# --- Who gets it ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_usage_is_purchases_minus_refunds_last_month(big_spender):
    w = big_spender
    assert preferential.usage_last_month(w.developer.pk) == Decimal("120.00")
    assert preferential.is_eligible(w.developer.pk)
    # This month's spending counts next month.
    paid_now = timezone.now()
    ledger(w.account, [("DEPOSIT", "500.00", paid_now), ("PURCHASE", "-500.00", paid_now)])
    assert preferential.usage_last_month(w.developer.pk) == Decimal("120.00")
    # Deposits alone never earn it, nor do usages below the threshold.
    turn_on(w.coffee, threshold="120.01")
    assert not preferential.is_eligible(w.developer.pk)
    turn_on(w.coffee, threshold="120.00")
    assert preferential.is_eligible(w.developer.pk)
    PreferentialSettings.objects.filter(pk=1).update(enabled=False)
    assert not preferential.is_eligible(w.developer.pk)


@pytest.mark.django_db
def test_the_highest_level_reached_counts(big_spender):
    """Ada spent 120.00 last month."""
    w = big_spender
    levels = [
        {"threshold": "500.00", "items": 10},
        {"threshold": "50.00", "items": 1},
        {"threshold": "100.00", "items": 3},
    ]
    turn_on(w.coffee, levels=levels)
    assert PreferentialSettings.current().levels[0] == {"threshold": "50.00", "items": 1}
    assert preferential.earned(w.developer.pk) == 3
    assert preferential.items_for(PreferentialSettings.current().levels, Decimal("49.99")) == 0


# --- At the till ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_free_items_go_to_the_dearest_preferential_units(till, big_spender):
    w = big_spender
    before = balance(w)
    pid = new_purchase(till, w, (w.coffee, 3), (w.cake, 1), (w.tea, 1))
    r = confirm(till, pid)
    assert r.status_code == 201, r.json()
    body = r.json()
    free = {i["good_name"]: (i["free_quantity"], i["line_total"]) for i in body["items"]}
    # 2 items: the cake (4.00) first, then a coffee (3.00); tea is not preferential.
    assert free == {"Cake": (1, "0.00"), "Coffee": (1, "6.00"), "Tea": (0, "2.50")}
    assert body["total"] == "8.50"
    assert balance(w) == before - Decimal("8.50")
    assert sorted(AuditLog.objects.get(action="purchase.confirmed").new_values["free"]) == sorted(
        [[w.cake.pk, 1], [w.coffee.pk, 1]]
    )

    # Used up.
    pid = new_purchase(till, w, (w.coffee, 1))
    assert confirm(till, pid).json()["total"] == "3.00"
    assert preferential.free_left(w.developer.pk) == 0
    assert_books_balanced()


@pytest.mark.django_db
def test_not_eligible_pays_full_price(till, world):
    turn_on(world.coffee)
    pid = new_purchase(till, world, (world.coffee, 1))
    assert confirm(till, pid).json()["total"] == "3.00"
    assert PurchaseItem.objects.get().free_quantity == 0


@pytest.mark.django_db
def test_unused_free_items_add_up(till, world):
    """Set up 40 days before this month; Ada spent 150.00 two months ago and last month,
    so she earned 2 items last month and 2 this month: 4 saved up."""
    spent_last_month(world.account, "150.00", days_before_this_month=50)
    spent_last_month(world.account, "150.00", days_before_this_month=4)
    turn_on(world.coffee)
    PreferentialRule.objects.update(created_at=last_month(40))
    # Raising the threshold now only changes this month: last month keeps its 2.
    turn_on(world.coffee, threshold="200.00")
    assert preferential.earned(world.developer.pk) == 2
    turn_on(world.coffee)
    assert preferential.earned(world.developer.pk) == 4

    pid = new_purchase(till, world, (world.coffee, 3))
    assert confirm(till, pid).json()["total"] == "0.00"
    assert preferential.free_left(world.developer.pk) == 1
    # Next month without spending: nothing new, but the saved one is still there.
    next_month = preferential.month_bounds()[2] + timedelta(days=1)
    assert preferential.free_left(world.developer.pk, now=next_month) == 1
    assert_books_balanced()


@pytest.mark.django_db
def test_an_all_free_sale_needs_card_and_pin_but_no_money(till, big_spender):
    w = big_spender
    before = balance(w)
    pid = new_purchase(till, w, (w.coffee, 2))
    assert confirm(till, pid, pin="0000").json()["error"]["code"] == "INVALID_PIN"
    r = confirm(till, pid)
    assert r.status_code == 201, r.json()
    assert (r.json()["total"], r.json()["status"]) == ("0.00", "CONFIRMED")
    assert r.json()["balance_after"] is None
    assert balance(w) == before
    assert not AccountTransaction.objects.filter(reference=f"purchase:{pid}").exists()

    # Deleting it moves no money, and the free units stay used.
    assert till.delete(f"{PURCHASES}{pid}/").status_code in (200, 204)
    assert balance(w) == before
    assert preferential.free_left(w.developer.pk) == 0
    assert_books_balanced()


@pytest.mark.django_db
def test_refund_returns_the_money_paid_not_the_free_units(till, big_spender):
    w = big_spender
    before = balance(w)
    pid = paid(till, w, (w.coffee, 3))  # 2 free, 1 paid
    till.delete(f"{PURCHASES}{pid}/")
    assert balance(w) == before
    assert preferential.used(w.developer.pk) == 2
    assert_books_balanced()


@pytest.mark.django_db
def test_changing_a_paid_sale_keeps_its_free_units(till, big_spender):
    w = big_spender
    before = balance(w)
    pid = paid(till, w, (w.coffee, 3), (w.tea, 1))  # pays 3.00 + 2.50
    # Fewer coffees: the paid one goes back first.
    r = till.patch(f"{PURCHASES}{pid}/", lines((w.coffee, 2), (w.tea, 1)), format="json")
    assert r.status_code == 200, r.json()
    assert r.json()["total"] == "2.50"
    assert balance(w) == before - Decimal("2.50")
    # Only free coffees left: a total of 0 is fine.
    r = till.patch(f"{PURCHASES}{pid}/", lines((w.coffee, 1)), format="json")
    assert r.status_code == 200, r.json()
    assert r.json()["total"] == "0.00"
    assert balance(w) == before
    # Used is used: the coffee given back stays counted.
    assert preferential.used(w.developer.pk) == 2
    assert_books_balanced()


@pytest.mark.django_db
def test_the_till_shows_free_units_once_the_card_is_tapped(till, big_spender):
    w = big_spender
    pid = new_purchase(till, w, (w.coffee, 3))
    assert till.get(f"{PURCHASES}{pid}/").json()["total"] == "9.00"
    tap_on_till(till, w, pid)
    body = till.get(f"{PURCHASES}{pid}/").json()
    assert body["presented_card"]["developer"]["full_name"] == "Ada"
    assert (body["items"][0]["free_quantity"], body["total"]) == (2, "3.00")
    # The pay screen shows the items before this sale and the balance.
    assert body["presented_card"]["free_items"] == 2
    assert body["presented_card"]["balance"] == "50.00"


# --- Settings and the developer's page -----------------------------------------------------


@pytest.mark.django_db
def test_only_admins_change_the_settings(auth_client, make_user, world):
    boss = auth_client(make_user(Roles.BOSS))
    assert boss.get(SETTINGS).json()["enabled"] is False
    body = {
        "enabled": True,
        "levels": [{"threshold": "300.00", "items": 5}, {"threshold": "100.00", "items": 3}],
        "goods": [world.coffee.pk],
    }
    assert boss.put(SETTINGS, body, format="json").status_code == 403
    finance_manager = auth_client(make_user(Roles.FINANCE_MANAGER))
    assert finance_manager.put(SETTINGS, body, format="json").status_code == 403

    admin = auth_client(make_user(Roles.ADMIN))
    for levels in (
        [],
        [{"threshold": "0", "items": 2}],
        [{"threshold": "100.00", "items": 0}],
        [{"threshold": "100.00", "items": 1}, {"threshold": "100", "items": 2}],
    ):
        bad = admin.put(SETTINGS, body | {"levels": levels}, format="json")
        assert bad.status_code == 400, levels
    assert admin.put(SETTINGS, body | {"goods": []}, format="json").status_code == 400

    r = admin.put(SETTINGS, body, format="json")
    assert r.status_code == 200, r.json()
    assert r.json()["levels"] == [
        {"threshold": "100.00", "items": 3},
        {"threshold": "300.00", "items": 5},
    ]
    assert r.json()["goods"] == [
        {
            "good": world.coffee.pk,
            "good_name": "Coffee",
            "kind": "PRODUCT",
            "price": "3.00",
            "seller_name": "Cafe",
        }
    ]
    # The list is replaced as a whole.
    r = admin.put(SETTINGS, body | {"goods": [world.tea.pk]}, format="json")
    assert [g["good"] for g in r.json()["goods"]] == [world.tea.pk]
    assert AuditLog.objects.filter(action="purchase.preferential_changed").count() == 2


@pytest.mark.django_db
def test_developer_page_shows_usage_and_what_is_left(auth_client, make_user, till, big_spender):
    w = big_spender
    paid(till, w, (w.coffee, 1))
    finance_manager = auth_client(make_user(Roles.FINANCE_MANAGER))
    r = finance_manager.get(f"/api/v1/finance/accounts/{w.account.pk}/preferential/")
    assert r.status_code == 200, r.json()
    assert r.json() == {
        "enabled": True,
        "levels": [{"threshold": "100.00", "items": 2}],
        "usage_last_month": "120.00",
        "eligible": True,
        "items_this_month": 2,
        "earned": 2,
        "used": 1,
        "left": 1,
    }
    seller = auth_client(w.seller_user)
    assert seller.get(f"/api/v1/finance/accounts/{w.account.pk}/preferential/").status_code in (
        403,
        404,
    )


@pytest.mark.django_db
def test_report_of_free_goods_given(auth_client, make_user, till, big_spender):
    w = big_spender
    pid = paid(till, w, (w.coffee, 3))  # 2 free
    till.delete(f"{PURCHASES}{pid}/")  # deleted: still given
    boss = auth_client(make_user(Roles.BOSS))
    rows = boss.get("/api/v1/preferential/report/").json()
    assert [(r["full_name"], r["good_name"], r["units"], r["value"]) for r in rows] == [
        ("Ada", "Coffee", 2, "6.00")
    ]
    excel = boss.get("/api/v1/preferential/report/export/")
    assert excel.status_code == 200
    assert excel["Content-Type"].startswith("application/vnd.openxmlformats")
    # The goods list says which goods are on the free list.
    goods = {g["name"]: g["preferential"] for g in boss.get("/api/v1/goods/").json()["results"]}
    assert (goods["Coffee"], goods["Tea"]) == (True, False)
    assert auth_client(w.seller_user).get("/api/v1/preferential/report/").status_code == 403


@pytest.mark.django_db
def test_seed_command_turns_it_on_and_lists_developers(settings, big_spender):
    import io

    from django.core.management import call_command

    settings.TEST_CONSOLE_ENABLED = True
    out = io.StringIO()
    call_command("seed_preferential", "--levels", "50:1,100:3", "--goods", "2", stdout=out)
    current = PreferentialSettings.current()
    assert current.enabled and current.levels[-1] == {"threshold": "100.00", "items": 3}
    assert "Ada: card 04AABBCCDD, spent 120.00, 3 left" in out.getvalue()
    call_command("seed_preferential", "--undo", stdout=io.StringIO())
    assert not PreferentialSettings.current().enabled
    assert preferential.earned(big_spender.developer.pk) == 0


@pytest.mark.django_db
def test_wallet_list_shows_free_items_left(auth_client, make_user, till, big_spender):
    w = big_spender
    paid(till, w, (w.coffee, 1))
    finance_manager = auth_client(make_user(Roles.FINANCE_MANAGER))
    rows = finance_manager.get("/api/v1/finance/accounts/").json()["results"]
    assert [(r["developer"]["full_name"], r["free_items"]) for r in rows] == [("Ada", 1)]
    PreferentialSettings.objects.filter(pk=1).update(enabled=False)
    rows = finance_manager.get("/api/v1/finance/accounts/").json()["results"]
    assert rows[0]["free_items"] is None
