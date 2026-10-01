import pytest

from apps.accounts.rbac import Roles
from apps.audit.models import AuditLog
from apps.developers.models import Developer, DeveloperStatus

pytestmark = pytest.mark.django_db

URL = "/api/v1/developers/"


def payload(n=1, **extra):
    return {
        "employee_number": f"E{n:04d}",
        "full_name": f"Dev {n}",
        "department": "Engineering",
        **extra,
    }


@pytest.fixture
def manager_client(auth_client, make_user):
    return auth_client(make_user(Roles.MANAGER))


@pytest.fixture
def make_developer():
    counter = iter(range(1000, 100_000))

    def _make(**extra):
        n = next(counter)
        data = payload(n) | extra
        return Developer.objects.create(**data)

    return _make


def test_create_developer_is_audited(manager_client):
    response = manager_client.post(
        URL,
        payload(1, home_address="1 Main St", birthday="1990-05-17", start_date="2020-01-06"),
    )
    assert response.status_code == 201, response.json()
    body = response.json()
    assert (body["home_address"], body["birthday"], body["out_date"]) == (
        "1 Main St",
        "1990-05-17",
        None,
    )
    assert body["status"] == DeveloperStatus.ACTIVE
    assert "email" not in body

    log = AuditLog.objects.get(action="developer.created")
    assert log.entity_id == str(body["id"])
    assert log.new_values["employee_number"] == "E0001"


@pytest.mark.parametrize(
    ("role", "list_status", "create_status"),
    [
        (Roles.BOSS, 200, 201),
        (Roles.MANAGER, 200, 201),
        (Roles.FINANCE_MANAGER, 200, 403),
        (Roles.DEVELOPER, 403, 403),
        (Roles.SELLER, 403, 403),
    ],
)
def test_access_by_role(auth_client, make_user, role, list_status, create_status):
    client = auth_client(make_user(role))
    assert client.get(URL).status_code == list_status
    assert client.post(URL, payload(1)).status_code == create_status


def test_employee_number_must_be_unique(manager_client, make_developer):
    make_developer(employee_number="E0001")
    response = manager_client.post(URL, payload(1))
    assert response.status_code == 400
    assert set(response.json()["error"]["details"]) == {"employee_number"}


def test_out_date_cannot_precede_start_date(manager_client, make_developer):
    response = manager_client.post(URL, payload(1, start_date="2024-03-01", out_date="2024-02-29"))
    assert "out_date" in response.json()["error"]["details"]

    # Also checked against the stored start date on PATCH, and by the database.
    dev = make_developer(start_date="2024-03-01")
    response = manager_client.patch(f"{URL}{dev.pk}/", {"out_date": "2024-01-01"})
    assert response.status_code == 400
    assert manager_client.patch(f"{URL}{dev.pk}/", {"out_date": "2025-06-30"}).status_code == 200


def test_database_enforces_out_date_order(make_developer):
    from django.db import IntegrityError, transaction

    dev = make_developer(start_date="2024-03-01")
    with pytest.raises(IntegrityError), transaction.atomic():
        Developer.objects.filter(pk=dev.pk).update(out_date="2024-01-01")


def test_birthday_cannot_be_in_future(manager_client):
    response = manager_client.post(URL, payload(1, birthday="2999-01-01"))
    assert "birthday" in response.json()["error"]["details"]


def test_new_fields_are_audited_and_filterable(manager_client, make_developer):
    dev = make_developer(birthday="1991-07-04")
    make_developer(birthday="1988-02-10")
    manager_client.patch(f"{URL}{dev.pk}/", {"out_date": "2026-12-31", "home_address": "2 Elm"})
    log = AuditLog.objects.get(action="developer.updated")
    assert log.new_values == {"home_address": "2 Elm", "out_date": "2026-12-31"}

    def ids(query):
        return [d["id"] for d in manager_client.get(f"{URL}?{query}").json()["results"]]

    assert ids("birthday_month=7") == [dev.pk]
    assert ids("out_after=2026-12-01") == [dev.pk]
    assert ids("out_before=2026-01-01") == []


def test_soft_deleted_developer_frees_identifiers(manager_client, make_developer):
    old = make_developer(employee_number="E0001")
    assert manager_client.delete(f"{URL}{old.pk}/").status_code == 204

    assert manager_client.get(f"{URL}{old.pk}/").status_code == 404
    assert Developer.all_objects.get(pk=old.pk).deleted_at is not None
    assert manager_client.post(URL, payload(1)).status_code == 201
    assert AuditLog.objects.filter(action="developer.deleted", entity_id=str(old.pk)).exists()


def test_cannot_delete_developer_with_reports(manager_client, make_developer):
    boss = make_developer()
    make_developer(manager=boss)
    response = manager_client.delete(f"{URL}{boss.pk}/")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DEVELOPER_HAS_REPORTS"


def test_manager_cycle_is_rejected(manager_client, make_developer):
    top = make_developer()
    middle = make_developer(manager=top)
    bottom = make_developer(manager=middle)

    # Self-management and an indirect cycle (top → bottom → middle → top).
    for new_manager in (top, bottom):
        response = manager_client.patch(f"{URL}{top.pk}/", {"manager": new_manager.pk})
        assert response.status_code == 400, new_manager
        assert "manager" in response.json()["error"]["details"]


def test_deleted_developer_cannot_be_manager(manager_client, make_developer):
    gone = make_developer()
    gone.soft_delete()
    response = manager_client.post(URL, payload(1, manager=gone.pk))
    assert response.status_code == 400


def test_update_audits_only_changed_fields(manager_client, make_developer):
    dev = make_developer(department="Engineering")
    response = manager_client.patch(
        f"{URL}{dev.pk}/", {"department": "Research", "full_name": dev.full_name}
    )
    assert response.status_code == 200
    log = AuditLog.objects.get(action="developer.updated")
    assert log.old_values == {"department": "Engineering"}
    assert log.new_values == {"department": "Research"}


def test_noop_update_writes_no_audit(manager_client, make_developer):
    dev = make_developer()
    manager_client.patch(f"{URL}{dev.pk}/", {"full_name": dev.full_name})
    assert not AuditLog.objects.filter(action="developer.updated").exists()


def test_user_can_link_to_only_one_developer(manager_client, make_developer, make_user):
    user = make_user()
    make_developer(user=user)
    response = manager_client.post(URL, payload(1, user=user.pk))
    assert response.status_code == 400
    assert "user" in response.json()["error"]["details"]


def test_filters_search_and_ordering(manager_client, make_developer):
    make_developer(full_name="Alice Smith", department="Engineering")
    make_developer(full_name="Bob Jones", department="Research", status=DeveloperStatus.ON_LEAVE)
    make_developer(full_name="Carol Smith", department="engineering")

    def names(query):
        return [d["full_name"] for d in manager_client.get(f"{URL}?{query}").json()["results"]]

    assert names("status=ON_LEAVE") == ["Bob Jones"]
    assert names("department=Engineering") == ["Alice Smith", "Carol Smith"]
    assert names("search=smith&ordering=-full_name") == ["Carol Smith", "Alice Smith"]


def test_me_returns_own_profile(auth_client, make_user, make_developer):
    user = make_user(Roles.DEVELOPER)
    dev = make_developer(user=user)
    response = auth_client(user).get(f"{URL}me/")
    assert response.status_code == 200
    assert response.json()["id"] == dev.pk


def test_me_without_profile_is_404(auth_client, make_user):
    response = auth_client(make_user(Roles.SELLER)).get(f"{URL}me/")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DEVELOPER_PROFILE_NOT_FOUND"


def test_put_is_not_allowed(manager_client, make_developer):
    dev = make_developer()
    assert manager_client.put(f"{URL}{dev.pk}/", payload(1)).status_code in (403, 405)
