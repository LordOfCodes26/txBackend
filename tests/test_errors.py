import pytest
from rest_framework.test import APIRequestFactory

from common.exceptions import DomainError, exception_handler


class InsufficientBalance(DomainError):
    status_code = 409
    code = "INSUFFICIENT_BALANCE"
    default_detail = "Developer account has insufficient balance."


def test_domain_error_envelope():
    response = exception_handler(InsufficientBalance(details={"shortfall": "12.50"}), {})
    assert response.status_code == 409
    assert response.data == {
        "error": {
            "code": "INSUFFICIENT_BALANCE",
            "message": "Developer account has insufficient balance.",
            "details": {"shortfall": "12.50"},
        }
    }


def test_unhandled_exception_becomes_500_envelope():
    request = APIRequestFactory().get("/")
    response = exception_handler(RuntimeError("boom"), {"request": request})
    assert response.status_code == 500
    assert response.data["error"]["code"] == "INTERNAL_ERROR"
    assert "boom" not in str(response.data)


@pytest.mark.django_db
def test_unknown_resource_is_404_envelope(auth_client, boss):
    body = auth_client(boss).get("/api/v1/users/999999/").json()
    assert body["error"]["code"] == "NOT_FOUND"


@pytest.mark.django_db
def test_unique_violation_race_becomes_409():
    from django.db import IntegrityError, transaction

    from apps.accounts.models import User

    User.objects.create_user(email="dup@example.com", password="x")
    try:
        with transaction.atomic():
            User.objects.create(email="dup@example.com")
    except IntegrityError as exc:
        response = exception_handler(exc, {})
    assert response.status_code == 409
    assert response.data["error"]["code"] == "CONFLICT"
