import pytest

from apps.audit.models import AuditLog

from .conftest import PASSWORD

pytestmark = pytest.mark.django_db


def login(client, email, password=PASSWORD):
    return client.post("/api/v1/auth/token/", {"email": email, "password": password})


def test_login_refresh_logout_flow(api_client, make_user):
    user = make_user(email="dev@example.com")

    tokens = login(api_client, "dev@example.com").json()
    assert {"access", "refresh"} <= tokens.keys()

    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
    me = api_client.get("/api/v1/auth/me/").json()
    assert me["id"] == user.id
    assert me["permissions"] == []

    rotated = api_client.post("/api/v1/auth/token/refresh/", {"refresh": tokens["refresh"]})
    assert rotated.status_code == 200
    # Rotation blacklists the old refresh token.
    reused = api_client.post("/api/v1/auth/token/refresh/", {"refresh": tokens["refresh"]})
    assert reused.status_code == 401

    new_refresh = rotated.json()["refresh"]
    assert api_client.post("/api/v1/auth/logout/", {"refresh": new_refresh}).status_code == 204
    after_logout = api_client.post("/api/v1/auth/token/refresh/", {"refresh": new_refresh})
    assert after_logout.status_code == 401


def test_login_is_case_insensitive_on_email(api_client, make_user):
    make_user(email="dev@example.com")
    assert login(api_client, "DEV@Example.com").status_code == 200


def test_wrong_password_uses_error_envelope(api_client, make_user):
    make_user(email="dev@example.com")
    response = login(api_client, "dev@example.com", "nope")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "NO_ACTIVE_ACCOUNT"


def test_inactive_user_cannot_login(api_client, make_user):
    make_user(email="dev@example.com", is_active=False)
    assert login(api_client, "dev@example.com").status_code == 401


def test_unauthenticated_request_uses_error_envelope(api_client):
    response = api_client.get("/api/v1/auth/me/")
    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "NOT_AUTHENTICATED",
            "message": "Authentication credentials were not provided.",
        }
    }


def test_invalid_token_uses_error_envelope(api_client):
    api_client.credentials(HTTP_AUTHORIZATION="Bearer garbage")
    body = api_client.get("/api/v1/auth/me/").json()
    assert body["error"]["code"] == "TOKEN_NOT_VALID"


def test_cannot_logout_someone_elses_refresh_token(api_client, make_user):
    make_user(email="a@example.com")
    make_user(email="b@example.com")
    a_tokens = login(api_client, "a@example.com").json()
    b_tokens = login(api_client, "b@example.com").json()

    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {a_tokens['access']}")
    response = api_client.post("/api/v1/auth/logout/", {"refresh": b_tokens["refresh"]})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_password_change_revokes_refresh_tokens_and_audits(api_client, make_user):
    user = make_user(email="dev@example.com")
    tokens = login(api_client, "dev@example.com").json()
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    response = api_client.post(
        "/api/v1/auth/password/",
        {"old_password": PASSWORD, "new_password": "An0ther-long-secret"},
    )
    assert response.status_code == 204
    refresh = api_client.post("/api/v1/auth/token/refresh/", {"refresh": tokens["refresh"]})
    assert refresh.status_code == 401
    assert login(api_client, "dev@example.com", "An0ther-long-secret").status_code == 200
    assert AuditLog.objects.filter(action="user.password_changed", entity_id=str(user.id)).exists()


def test_password_change_rejects_wrong_old_password(auth_client, make_user):
    client = auth_client(make_user())
    response = client.post(
        "/api/v1/auth/password/", {"old_password": "wrong", "new_password": "An0ther-long-secret"}
    )
    assert response.status_code == 400
    assert "old_password" in response.json()["error"]["details"]


def test_logout_works_after_access_token_expired(api_client, make_user):
    make_user(email="dev@example.com")
    tokens = login(api_client, "dev@example.com").json()
    api_client.credentials()  # no access token at all
    response = api_client.post("/api/v1/auth/logout/", {"refresh": tokens["refresh"]})
    assert response.status_code == 204
    refresh = api_client.post("/api/v1/auth/token/refresh/", {"refresh": tokens["refresh"]})
    assert refresh.status_code == 401


def test_logout_rejects_invalid_refresh_token(api_client):
    response = api_client.post("/api/v1/auth/logout/", {"refresh": "not-a-token"})
    assert response.status_code == 400
