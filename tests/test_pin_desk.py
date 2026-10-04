"""PIN desk: the developer (identified by a card tap) changes or resets their PIN."""

import pytest
from django.contrib.auth.hashers import check_password

from apps.accounts.rbac import Roles
from apps.audit.models import AuditLog
from apps.developers.models import Developer
from apps.finance import services
from apps.finance.models import DeveloperAccount

ACCOUNTS = "/api/v1/finance/accounts/"


@pytest.fixture
def account(db):
    dev = Developer.objects.create(employee_number="E1", full_name="Ada")
    account = services.open_account(dev)
    services.give_pin(actor=None, account=account, pin="4826", action="test")
    return account


@pytest.fixture
def desk(auth_client, make_user):
    return auth_client(make_user(Roles.FINANCE_MANAGER))


def change(desk, account, current, new, confirm=None):
    body = {"current_pin": current, "pin": new, "pin_confirm": confirm or new}
    return desk.post(f"{ACCOUNTS}{account.pk}/change-pin/", body)


def test_change_with_the_current_pin(desk, account):
    assert change(desk, account, "4826", "5093").status_code == 204
    account.refresh_from_db()
    assert check_password("5093", account.pin_hash)
    log = AuditLog.objects.get(action="finance.pin_changed_at_desk")
    assert "5093" not in str(log.new_values) and "4826" not in str(log.old_values)


def test_wrong_current_pin_counts_and_locks(desk, account, settings):
    settings.PURCHASE_PIN_MAX_ATTEMPTS = 3
    for _ in range(2):
        r = change(desk, account, "1111", "5093")
        assert r.json()["error"]["code"] == "INVALID_PIN"
    r = change(desk, account, "1111", "5093")  # third wrong one locks the PIN
    assert r.json()["error"]["code"] in ("INVALID_PIN", "PIN_LOCKED")
    r = change(desk, account, "4826", "5093")  # even the right one: locked now
    assert r.json()["error"]["code"] == "PIN_LOCKED"
    account.refresh_from_db()
    assert check_password("4826", account.pin_hash)


def test_new_pin_rules_and_confirmation(desk, account):
    r = change(desk, account, "4826", "5093", confirm="5094")
    assert "pin_confirm" in r.json()["error"]["details"]
    account.refresh_from_db()
    assert account.pin_failed_attempts == 0  # rejected before the current PIN is tried
    r = change(desk, account, "4826", "1234")
    assert "pin" in r.json()["error"]["details"]
    assert check_password("4826", DeveloperAccount.objects.get(pk=account.pk).pin_hash)


def test_reset_a_forgotten_pin_clears_the_lock(desk, account):
    DeveloperAccount.objects.filter(pk=account.pk).update(pin_failed_attempts=5)
    r = desk.post(f"{ACCOUNTS}{account.pk}/reset-pin/", {"pin": "7391", "pin_confirm": "7391"})
    assert r.status_code == 200 and r.json()["has_pin"] is True
    account.refresh_from_db()
    assert check_password("7391", account.pin_hash) and account.pin_failed_attempts == 0
    r = desk.post(f"{ACCOUNTS}{account.pk}/reset-pin/", {"pin": "7391", "pin_confirm": "7392"})
    assert "pin_confirm" in r.json()["error"]["details"]


def test_only_desk_staff(auth_client, make_user, account):
    manager = auth_client(make_user(Roles.MANAGER))
    assert change(manager, account, "4826", "5093").status_code == 403
