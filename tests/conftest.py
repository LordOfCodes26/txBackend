import pytest
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserRole
from apps.accounts.rbac import Roles

PASSWORD = "Str0ng-pass-phrase!"


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def make_user(db):
    counter = iter(range(1, 10_000))

    def _make(*roles: str, email: str | None = None, **extra) -> User:
        user = User.objects.create_user(
            email=email or f"user{next(counter)}@example.com", password=PASSWORD, **extra
        )
        for code in roles:
            UserRole.objects.create(user=user, role=Role.objects.get(code=code))
        return user

    return _make


@pytest.fixture
def boss(make_user):
    return make_user(Roles.BOSS, email="boss@example.com")


@pytest.fixture
def auth_client(api_client):
    """Returns a function that authenticates the shared client as `user`."""

    def _as(user: User) -> APIClient:
        api_client.force_authenticate(user)
        return api_client

    return _as
