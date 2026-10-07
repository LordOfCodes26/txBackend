"""Finer permissions, editing roles, and the permission catalog."""

import importlib

import pytest
from django.apps import apps as django_apps

from apps.accounts.models import Permission, Role
from apps.accounts.rbac import AREAS, PERMISSIONS, Roles
from apps.audit.models import AuditLog
from apps.sellers.models import Seller, ServicePosition

pytestmark = pytest.mark.django_db

ROLES = "/api/v1/roles/"


def codes(role_code):
    return set(Role.objects.get(code=role_code).permissions.values_list("codename", flat=True))


@pytest.fixture
def admin(auth_client, make_user):
    return auth_client(make_user(Roles.ADMIN, username="root"))


# --- The migration keeps every role working -------------------------------------------


def test_migration_splits_old_permissions_for_custom_roles():
    migration = importlib.import_module("apps.accounts.migrations.0005_finer_permissions")
    old = {
        c: Permission.objects.get_or_create(codename=c)[0]
        for c in ["rfid.view", "rfid.assign", "purchase.view", "seller.update", "finance.deposit"]
    }
    guard = Role.objects.create(code="DOOR_DESK", name="Door desk")
    guard.permissions.set(old.values())

    migration.forward(django_apps, None)

    assert {
        "card.view",
        "reader.view",
        "scan.view",
        "card.register",
        "card.assign",
        "booking.view",
        "counter.manage",
        "excel.export",
        "excel.import",
    } <= codes("DOOR_DESK")
    assert not Permission.objects.filter(codename__startswith="rfid.").exists()


def test_catalog_lists_every_permission_once_by_area(admin):
    body = admin.get("/api/v1/permissions/").json()
    assert [area["area"] for area in body] == list(AREAS)
    listed = [p["codename"] for area in body for p in area["permissions"]]
    assert sorted(listed) == sorted(PERMISSIONS) and len(listed) == len(set(listed))


# --- Editing roles ------------------------------------------------------------------


def test_create_change_and_delete_a_custom_role(admin):
    r = admin.post(
        ROLES,
        {"code": "NIGHT_GUARD", "name": "Night guard", "permissions": ["scan.view", "card.view"]},
        format="json",
    )
    assert r.status_code == 201, r.json()
    assert sorted(r.json()["permissions"]) == ["card.view", "scan.view"]

    r = admin.patch(f"{ROLES}NIGHT_GUARD/", {"permissions": ["scan.view"]}, format="json")
    assert r.json()["permissions"] == ["scan.view"]
    log = AuditLog.objects.get(action="role.updated")
    assert log.new_values == {"permissions": ["scan.view"]}

    assert admin.delete(f"{ROLES}NIGHT_GUARD/").status_code == 204
    assert not Role.objects.filter(code="NIGHT_GUARD").exists()


def test_admin_role_keeps_everything_and_builtins_stay(admin):
    r = admin.patch(f"{ROLES}ADMIN/", {"permissions": ["user.view"]}, format="json")
    assert r.status_code == 409 and r.json()["error"]["code"] == "ROLE_LOCKED"
    assert admin.delete(f"{ROLES}BOSS/").json()["error"]["code"] == "SYSTEM_ROLE"


def test_role_in_use_cannot_be_deleted(admin, make_user):
    Role.objects.create(code="TEMP", name="Temp")
    make_user("TEMP")
    assert admin.delete(f"{ROLES}TEMP/").json()["error"]["code"] == "ROLE_IN_USE"


def test_nobody_grants_what_they_do_not_hold(auth_client, make_user):
    # A role manager without finance rights cannot hand out deposits.
    manager_role = Role.objects.create(code="ROLE_ADMIN", name="Role admin")
    manager_role.permissions.set(
        Permission.objects.filter(codename__in=["role.view", "role.manage", "card.view"])
    )
    client = auth_client(make_user("ROLE_ADMIN"))
    r = client.post(
        ROLES,
        {"code": "CASHIER", "name": "Cashier", "permissions": ["finance.deposit"]},
        format="json",
    )
    assert r.status_code == 403 and not Role.objects.filter(code="CASHIER").exists()
    ok = client.post(
        ROLES, {"code": "VIEWER", "name": "Viewer", "permissions": ["card.view"]}, format="json"
    )
    assert ok.status_code == 201


def test_role_manage_is_needed(auth_client, make_user):
    client = auth_client(make_user(Roles.MANAGER))  # can see roles, not change them
    assert client.get(ROLES).status_code == 200
    assert client.patch(f"{ROLES}BOSS/", {"name": "Chief"}, format="json").status_code == 403


# --- The finer permissions take effect ----------------------------------------------------


def test_court_needs_court_manage(auth_client, make_user):
    position = ServicePosition.objects.create(
        seller=Seller.objects.create(name="Park"), name="Desk"
    )
    shop = Role.objects.create(code="SHOP_STAFF", name="Shop staff")
    shop.permissions.set(Permission.objects.filter(codename__in=["good.view", "good.create"]))
    staff = make_user("SHOP_STAFF")
    client = auth_client(staff)
    court = {
        "service_position": position.pk,
        "name": "Court",
        "price": "10.00",
        "kind": "RENTAL",
        "rental": {"slot_minutes": 60, "opening_time": "08:00", "closing_time": "20:00"},
    }
    assert client.post("/api/v1/goods/", court, format="json").status_code == 403
    tea = {"service_position": position.pk, "name": "Tea", "price": "2.00"}
    assert client.post("/api/v1/goods/", tea, format="json").status_code == 201

    shop.permissions.add(Permission.objects.get(codename="court.manage"))
    staff.invalidate_rbac_cache()  # the test reuses one user object
    assert client.post("/api/v1/goods/", court, format="json").status_code == 201


def test_excel_needs_its_own_permissions(auth_client, make_user):
    viewer = Role.objects.create(code="CARD_VIEWER", name="Card viewer")
    viewer.permissions.set(Permission.objects.filter(codename="card.view"))
    person = make_user("CARD_VIEWER")
    client = auth_client(person)
    assert client.get("/api/v1/rfid/cards/").status_code == 200
    assert client.get("/api/v1/rfid/cards/export/").status_code == 403
    viewer.permissions.add(Permission.objects.get(codename="excel.export"))
    person.invalidate_rbac_cache()
    assert client.get("/api/v1/rfid/cards/export/").status_code == 200
    # Readers are a separate right now.
    assert client.get("/api/v1/rfid/devices/").status_code == 403
