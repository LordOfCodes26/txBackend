"""seed_minus_wallets: test wallets below zero, and --undo."""

from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.developers.models import Developer
from apps.finance import services as finance
from apps.finance.models import DeveloperAccount, WalletSettings
from apps.rfid.models import Building


def run(*args):
    out = StringIO()
    call_command("seed_minus_wallets", *args, stdout=out)
    return out.getvalue()


@pytest.fixture
def people(db, settings):
    settings.TEST_CONSOLE_ENABLED = True
    b1 = Building.objects.create(code="B1", name="Building 1")
    b2 = Building.objects.create(code="B2", name="Building 2")
    devs = []
    for n, building in enumerate([b1, b1, b2]):
        dev = Developer.objects.create(
            employee_number=f"E{n}", full_name=f"Dev {n}", building=building
        )
        finance.deposit(actor=None, developer=dev, amount="30.00", idempotency_key=f"seed-{n}")
        devs.append(dev)
    return devs


def balances(devs):
    return [DeveloperAccount.objects.get(developer=d).balance for d in devs]


def test_only_on_test_servers(db, settings):
    settings.DEBUG = False
    settings.TEST_CONSOLE_ENABLED = False
    with pytest.raises(CommandError):
        run()


def test_seeds_minus_wallets_spread_over_buildings_and_undoes(people):
    out = run("--count", "2")
    assert "Minus limit 0.00 -> 20.00" in out
    after = balances(people)
    minus = [b for b in after if b < 0]
    assert len(minus) == 2 and all(Decimal("-20.00") <= b <= Decimal("-1.00") for b in minus)
    # One from each building: Dev 0 (B1) and Dev 2 (B2).
    assert after[0] < 0 and after[2] < 0 and after[1] == Decimal("30.00")
    assert finance.ledger_mismatches() == []

    # Again: the wallets already done stay as they are; the next one is taken.
    run("--count", "1")
    again = balances(people)
    assert again[0] == after[0] and again[2] == after[2] and again[1] < 0

    out = run("--undo")
    assert "Reversed 3 wallet(s)." in out
    assert balances(people) == [Decimal("30.00")] * 3
    assert WalletSettings.current().minus_limit == Decimal("0.00")
    assert "Reversed 0 wallet(s)." in run("--undo")  # nothing twice
    assert finance.ledger_mismatches() == []


def test_keeps_a_higher_limit(people):
    WalletSettings.objects.update_or_create(pk=1, defaults={"minus_limit": "50.00"})
    out = run("--count", "1")
    assert "Minus limit" not in out.splitlines()[0]
    assert WalletSettings.current().minus_limit == Decimal("50.00")


def test_undo_keeps_a_limit_changed_since(people):
    run("--count", "1")
    finance.set_minus_limit(actor=None, minus_limit="50.00")  # an admin, later
    out = run("--undo")
    assert "Reversed 1 wallet(s)." in out and "Minus limit kept" in out
    assert WalletSettings.current().minus_limit == Decimal("50.00")
    assert balances(people) == [Decimal("30.00")] * 3
