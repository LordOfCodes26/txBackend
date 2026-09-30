from unittest import mock

import pytest


def test_health(client):
    assert client.get("/health/").json() == {"status": "ok"}


@pytest.mark.django_db
def test_health_db(client):
    assert client.get("/health/db/").status_code == 200


def test_health_redis_reports_failure(client):
    with mock.patch("common.health.redis.Redis.from_url", side_effect=ConnectionError):
        response = client.get("/health/redis/")
    assert response.status_code == 503
    assert response.json()["component"] == "redis"


def test_request_id_is_echoed(client):
    response = client.get("/health/", HTTP_X_REQUEST_ID="abc-123")
    assert response["X-Request-ID"] == "abc-123"


def test_invalid_request_id_is_replaced(client):
    response = client.get("/health/", HTTP_X_REQUEST_ID="bad id with spaces")
    assert response["X-Request-ID"] != "bad id with spaces"
    assert len(response["X-Request-ID"]) == 32


@pytest.mark.django_db
def test_api_docs_load_no_external_assets(client, django_user_model):
    # The server may be offline, so the docs page must not reference a CDN.
    body = client.get("/api/docs/").content.decode()
    assert "cdn.jsdelivr.net" not in body
    assert "/static/drf_spectacular_sidecar/" in body
