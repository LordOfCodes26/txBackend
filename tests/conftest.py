import pytest
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserRole
from apps.accounts.rbac import Roles

PASSWORD = "Str0ng-pass-phrase!"


@pytest.fixture(autouse=True)
def _clear_cache():
    """Throttle and lockout counters live in the cache; don't leak them between tests."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _reset_language():
    """A request with Accept-Language leaves its language active in the test thread."""
    from django.utils import translation

    yield
    translation.deactivate()


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def make_user(db):
    counter = iter(range(1, 10_000))

    def _make(*roles: str, username: str | None = None, email: str | None = None, **extra) -> User:
        # `email="cafe@x.com"` (older tests) becomes the username "cafe".
        name = username or (email.split("@")[0] if email else f"user{next(counter)}")
        user = User.objects.create_user(username=name, password=PASSWORD, **extra)
        for code in roles:
            UserRole.objects.create(user=user, role=Role.objects.get(code=code))
        return user

    return _make


@pytest.fixture
def admin(make_user):
    return make_user(Roles.ADMIN, username="root")


@pytest.fixture
def auth_client(api_client):
    """Returns a function that authenticates the shared client as `user`."""

    def _as(user: User) -> APIClient:
        api_client.force_authenticate(user)
        return api_client

    return _as
