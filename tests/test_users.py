import pytest

from apps.accounts.models import User
from apps.accounts.rbac import Roles
from apps.audit.models import AuditLog

pytestmark = pytest.mark.django_db


def test_create_user_is_audited(auth_client, boss):
    response = auth_client(boss).post(
        "/api/v1/users/",
        {"email": "New@Example.com", "full_name": "New Person", "password": "Very-secret-123"},
        HTTP_X_REQUEST_ID="req-1",
        HTTP_USER_AGENT="pytest",
    )
    assert response.status_code == 201, response.json()
    user = User.objects.get(pk=response.json()["id"])
    assert user.email == "new@example.com"
    assert "password" not in response.json()

    log = AuditLog.objects.get(action="user.created", entity_id=str(user.pk))
    assert log.actor == boss
    assert log.new_values["email"] == "new@example.com"
    assert (log.request_id, log.user_agent, log.ip_address) == ("req-1", "pytest", "127.0.0.1")


def test_create_user_rejects_duplicate_email_case_insensitively(auth_client, boss, make_user):
    make_user(email="taken@example.com")
    response = auth_client(boss).post(
        "/api/v1/users/", {"email": "TAKEN@example.com", "password": "Very-secret-123"}
    )
    assert response.status_code == 400
    assert "email" in response.json()["error"]["details"]


def test_create_user_enforces_password_validators(auth_client, boss):
    response = auth_client(boss).post(
        "/api/v1/users/", {"email": "x@example.com", "password": "123"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_list_supports_search_filter_and_pagination(auth_client, boss, make_user):
    make_user(Roles.DEVELOPER, email="alice@example.com", full_name="Alice")
    make_user(Roles.SELLER, email="bob@example.com", full_name="Bob")
    client = auth_client(boss)

    assert [u["email"] for u in client.get("/api/v1/users/?search=ali").json()["results"]] == [
        "alice@example.com"
    ]
    by_role = client.get("/api/v1/users/?role=SELLER").json()["results"]
    assert [u["email"] for u in by_role] == ["bob@example.com"]

    page = client.get("/api/v1/users/?page_size=1&ordering=email").json()
    assert page["count"] == 3
    assert page["results"][0]["email"] == "alice@example.com"
    assert page["next"] is not None


def test_deactivate_user_records_diff_and_revokes_tokens(api_client, auth_client, boss, make_user):
    dev = make_user(Roles.DEVELOPER, email="dev@example.com")
    from .conftest import PASSWORD

    refresh = api_client.post(
        "/api/v1/auth/token/", {"email": "dev@example.com", "password": PASSWORD}
    ).json()["refresh"]

    response = auth_client(boss).patch(f"/api/v1/users/{dev.pk}/", {"is_active": False})
    assert response.status_code == 200
    assert response.json()["is_active"] is False

    log = AuditLog.objects.get(action="user.updated", entity_id=str(dev.pk))
    assert log.old_values == {"is_active": True}
    assert log.new_values == {"is_active": False}

    api_client.force_authenticate(None)
    assert api_client.post("/api/v1/auth/token/refresh/", {"refresh": refresh}).status_code == 401


def test_noop_update_writes_no_audit(auth_client, boss, make_user):
    dev = make_user(full_name="Same")
    auth_client(boss).patch(f"/api/v1/users/{dev.pk}/", {"full_name": "Same"})
    assert not AuditLog.objects.filter(action="user.updated").exists()


def test_cannot_delete_users(auth_client, boss, make_user):
    dev = make_user()
    assert auth_client(boss).delete(f"/api/v1/users/{dev.pk}/").status_code in (403, 405)
    assert User.objects.filter(pk=dev.pk).exists()
