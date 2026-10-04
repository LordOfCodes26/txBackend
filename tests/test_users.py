import pytest

from apps.accounts.models import User
from apps.accounts.rbac import Roles
from apps.audit.models import AuditLog

pytestmark = pytest.mark.django_db


def test_create_user_is_audited(auth_client, admin):
    response = auth_client(admin).post(
        "/api/v1/users/",
        {"username": "New.Person", "full_name": "New Person", "password": "Very-secret-123"},
        HTTP_X_REQUEST_ID="req-1",
        HTTP_USER_AGENT="pytest",
    )
    assert response.status_code == 201, response.json()
    user = User.objects.get(pk=response.json()["id"])
    assert user.username == "new.person"  # stored lower-case
    assert "password" not in response.json()

    log = AuditLog.objects.get(action="user.created", entity_id=str(user.pk))
    assert log.actor == admin
    assert log.new_values["username"] == "new.person"
    assert (log.request_id, log.user_agent, log.ip_address) == ("req-1", "pytest", "127.0.0.1")


def test_create_user_rejects_duplicate_username_case_insensitively(auth_client, admin, make_user):
    make_user(username="taken")
    client = auth_client(admin)
    response = client.post("/api/v1/users/", {"username": "TAKEN", "password": "Very-secret-123"})
    assert response.status_code == 400
    assert "username" in response.json()["error"]["details"]
    for bad in ("ab", "has space", "a@b.com"):
        r = client.post("/api/v1/users/", {"username": bad, "password": "Very-secret-123"})
        assert "username" in r.json()["error"]["details"], bad


def test_admin_renames_a_user(auth_client, admin, make_user):
    user = make_user(username="kim")
    make_user(username="lee")
    client = auth_client(admin)
    r = client.patch(f"/api/v1/users/{user.pk}/", {"username": "Kim.Jin"})
    assert (r.status_code, r.json()["username"]) == (200, "kim.jin")
    assert (
        "username"
        in client.patch(f"/api/v1/users/{user.pk}/", {"username": "LEE"}).json()["error"]["details"]
    )
    log = AuditLog.objects.filter(action="user.updated", entity_id=str(user.pk)).latest("id")
    assert (log.old_values["username"], log.new_values["username"]) == ("kim", "kim.jin")


def test_create_user_enforces_password_validators(auth_client, admin):
    response = auth_client(admin).post("/api/v1/users/", {"username": "xavier", "password": "123"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_list_supports_search_filter_and_pagination(auth_client, admin, make_user):
    make_user(Roles.DEVELOPER, username="alice", full_name="Alice")
    make_user(Roles.SELLER, username="bob", full_name="Bob")
    client = auth_client(admin)

    assert [u["username"] for u in client.get("/api/v1/users/?search=ali").json()["results"]] == [
        "alice"
    ]
    by_role = client.get("/api/v1/users/?role=SELLER").json()["results"]
    assert [u["username"] for u in by_role] == ["bob"]

    page = client.get("/api/v1/users/?page_size=1&ordering=username").json()
    assert page["count"] == 3
    assert page["results"][0]["username"] == "alice"
    assert page["next"] is not None


def test_deactivate_user_records_diff_and_revokes_tokens(api_client, auth_client, admin, make_user):
    dev = make_user(Roles.DEVELOPER, username="dev")
    from .conftest import PASSWORD

    refresh = api_client.post(
        "/api/v1/auth/token/", {"username": "dev", "password": PASSWORD}
    ).json()["refresh"]

    response = auth_client(admin).patch(f"/api/v1/users/{dev.pk}/", {"is_active": False})
    assert response.status_code == 200
    assert response.json()["is_active"] is False

    log = AuditLog.objects.get(action="user.updated", entity_id=str(dev.pk))
    assert log.old_values == {"is_active": True}
    assert log.new_values == {"is_active": False}

    api_client.force_authenticate(None)
    assert api_client.post("/api/v1/auth/token/refresh/", {"refresh": refresh}).status_code == 401


def test_noop_update_writes_no_audit(auth_client, admin, make_user):
    dev = make_user(full_name="Same")
    auth_client(admin).patch(f"/api/v1/users/{dev.pk}/", {"full_name": "Same"})
    assert not AuditLog.objects.filter(action="user.updated").exists()


def test_cannot_delete_users(auth_client, admin, make_user):
    dev = make_user()
    assert auth_client(admin).delete(f"/api/v1/users/{dev.pk}/").status_code in (403, 405)
    assert User.objects.filter(pk=dev.pk).exists()
