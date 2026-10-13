"""An admin sets a new password for a user who forgot theirs."""

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import Permission, Role, UserRole
from apps.accounts.rbac import Roles
from apps.audit.models import AuditLog

from .conftest import PASSWORD

NEW = "Fresh-Pass-2026"
pytestmark = pytest.mark.django_db


def url(user):
    return f"/api/v1/users/{user.pk}/password/"


def login(username, password):
    return APIClient().post("/api/v1/auth/token/", {"username": username, "password": password})


def test_admin_resets_a_password(auth_client, make_user):
    admin = auth_client(make_user(Roles.ADMIN))
    kim = make_user(Roles.MANAGER, username="kim")
    refresh = login("kim", PASSWORD).json()["refresh"]

    r = admin.post(url(kim), {"new_password": NEW, "new_password_confirm": NEW})
    assert r.status_code == 204, r.content
    assert login("kim", PASSWORD).status_code != 200  # the old one no longer works
    assert login("kim", NEW).status_code == 200
    # Signed out everywhere: the old session can't be renewed.
    assert APIClient().post("/api/v1/auth/token/refresh/", {"refresh": refresh}).status_code != 200
    assert AuditLog.objects.filter(action="user.password_reset", entity_id=str(kim.pk)).exists()


def test_bad_passwords_and_self(auth_client, make_user):
    me = make_user(Roles.ADMIN)
    admin = auth_client(me)
    kim = make_user(Roles.MANAGER)
    r = admin.post(url(kim), {"new_password": NEW, "new_password_confirm": "Other-Pass-2026"})
    assert "new_password_confirm" in r.json()["error"]["details"]
    r = admin.post(url(kim), {"new_password": "12345678", "new_password_confirm": "12345678"})
    assert "new_password" in r.json()["error"]["details"] or r.status_code == 400
    r = admin.post(url(me), {"new_password": NEW, "new_password_confirm": NEW})
    assert r.status_code == 400  # your own: with your current password


def test_who_may_reset(auth_client, make_user):
    kim = make_user(Roles.MANAGER)
    body = {"new_password": NEW, "new_password_confirm": NEW}
    # Managers manage users' data but don't have this permission.
    assert auth_client(make_user(Roles.MANAGER)).post(url(kim), body).status_code == 403
    # Someone who may reset passwords can't take over a stronger account.
    helpdesk = Role.objects.create(code="HELPDESK", name="Helpdesk")
    helpdesk.permissions.add(
        *Permission.objects.filter(codename__in=["user.view", "user.password_reset"])
    )
    clerk = make_user()
    UserRole.objects.create(user=clerk, role=helpdesk)
    admin = make_user(Roles.ADMIN)
    assert auth_client(clerk).post(url(admin), body).status_code == 403
    assert login(admin.username, PASSWORD).status_code == 200  # unchanged
