"""manage.py create_role_users --domain chonha.com: one login per role."""

from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from apps.accounts.models import User
from apps.accounts.rbac import ROLES, Roles

pytestmark = pytest.mark.django_db


def run(*args):
    out = StringIO()
    call_command("create_role_users", *args, stdout=out)
    return out.getvalue()


def test_one_user_per_role_with_unique_passwords():
    out = run("--domain", "@Chonha.com")
    users = User.objects.filter(email__endswith="@chonha.com")
    assert users.count() == len(ROLES)
    for code in ROLES:
        user = users.get(email=f"{code.lower()}@chonha.com")
        assert list(user.user_roles.values_list("role__code", flat=True)) == [code]
        password = next(line.split()[-1] for line in out.splitlines() if user.email in line)
        assert user.check_password(password)
    assert User.objects.get(email="boss@chonha.com").has_rbac_perm("stats.view")


def test_rerun_skips_existing_users_and_keeps_their_passwords():
    run("--domain", "chonha.com")
    admin = User.objects.get(email="admin@chonha.com")
    before = admin.password
    out = run("--domain", "chonha.com")
    assert "No new users." in out and "exists, skipped" in out
    admin.refresh_from_db()
    assert admin.password == before


def test_only_some_roles():
    run("--domain", "chonha.com", "--roles", "boss", Roles.BUILDING_OWNER)
    assert sorted(User.objects.values_list("email", flat=True)) == [
        "boss@chonha.com",
        "building_owner@chonha.com",
    ]


def test_bad_input():
    with pytest.raises(CommandError, match="Unknown roles"):
        run("--domain", "chonha.com", "--roles", "KING")
    with pytest.raises(CommandError, match="valid email domain"):
        run("--domain", "chonha")
    assert not User.objects.exists()


def test_typed_password(monkeypatch):
    answers = iter(["Long-enough-pass-91", "Long-enough-pass-91"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    out = run("--domain", "chonha.com", "--roles", "ADMIN", "--ask-password")
    assert "Long-enough-pass-91" not in out
    assert User.objects.get(email="admin@chonha.com").check_password("Long-enough-pass-91")
