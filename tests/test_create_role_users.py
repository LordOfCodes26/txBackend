"""manage.py create_role_users [--prefix chonha_]: one login per role, named after it."""

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
    out = run()
    assert User.objects.count() == len(ROLES)
    for code in ROLES:
        user = User.objects.get(username=code.lower())
        assert list(user.user_roles.values_list("role__code", flat=True)) == [code]
        password = next(
            line.split()[-1] for line in out.splitlines() if line.split()[:1] == [user.username]
        )
        assert user.check_password(password)
    assert User.objects.get(username="boss").has_rbac_perm("stats.view")


def test_prefix():
    run("--prefix", "Chonha_", "--roles", "admin")
    assert list(User.objects.values_list("username", flat=True)) == ["chonha_admin"]


def test_rerun_skips_existing_users_and_keeps_their_passwords():
    run()
    admin = User.objects.get(username="admin")
    before = admin.password
    out = run()
    assert "No new users." in out and "exists, skipped" in out
    admin.refresh_from_db()
    assert admin.password == before


def test_only_some_roles():
    run("--roles", "boss", Roles.BUILDING_OWNER)
    assert sorted(User.objects.values_list("username", flat=True)) == ["boss", "building_owner"]


def test_bad_input():
    with pytest.raises(CommandError, match="Unknown roles"):
        run("--roles", "KING")
    with pytest.raises(CommandError, match="valid username prefix"):
        run("--prefix", "a b")
    assert not User.objects.exists()


def test_typed_password(monkeypatch):
    answers = iter(["Long-enough-pass-91", "Long-enough-pass-91"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    out = run("--roles", "ADMIN", "--ask-password")
    assert "Long-enough-pass-91" not in out
    assert User.objects.get(username="admin").check_password("Long-enough-pass-91")
