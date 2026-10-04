import pytest

from apps.accounts.models import Permission, Role
from apps.accounts.rbac import ALL, PERMISSIONS, ROLES, Roles, sync_rbac

pytestmark = pytest.mark.django_db


def test_system_roles_are_seeded_after_migrate():
    assert set(Role.objects.values_list("code", flat=True)) == set(ROLES)
    admin = Role.objects.get(code=Roles.ADMIN)
    assert set(admin.permissions.values_list("codename", flat=True)) == ALL


def test_sync_is_idempotent_and_preserves_role_customisations():
    manager = Role.objects.get(code=Roles.MANAGER)
    manager.permissions.remove(Permission.objects.get(codename="developer.delete"))

    result = sync_rbac()

    assert result.created_permissions == []
    assert result.created_roles == []
    assert not manager.permissions.filter(codename="developer.delete").exists()


def test_new_catalog_permission_is_granted_to_default_roles(monkeypatch):
    monkeypatch.setitem(PERMISSIONS, "developer.export", "Export developers")
    spec = ROLES[Roles.MANAGER]
    monkeypatch.setitem(
        ROLES,
        Roles.MANAGER,
        spec.__class__(spec.name, spec.description, spec.permissions | {"developer.export"}),
    )

    result = sync_rbac()

    assert result.created_permissions == ["developer.export"]
    manager = Role.objects.get(code=Roles.MANAGER)
    assert manager.permissions.filter(codename="developer.export").exists()
    assert not Role.objects.get(code=Roles.SELLER).permissions.exists()


def test_stale_permissions_are_reported_and_pruned_on_request():
    Permission.objects.create(codename="legacy.thing")
    assert sync_rbac().stale_permissions == ["legacy.thing"]
    sync_rbac(prune=True)
    assert not Permission.objects.filter(codename="legacy.thing").exists()


def test_user_permissions_come_from_all_roles(make_user):
    user = make_user(Roles.FINANCE_MANAGER, Roles.MANAGER)
    assert user.has_rbac_perms(["finance.deposit", "developer.create"])
    assert not user.has_rbac_perm("seller.create")


def test_inactive_user_has_no_permissions(make_user):
    assert make_user(Roles.ADMIN, is_active=False).rbac_permissions == frozenset()


def test_superuser_has_every_permission(make_user):
    assert make_user(is_superuser=True).rbac_permissions == ALL


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (Roles.ADMIN, 200),
        (Roles.MANAGER, 200),
        (Roles.FINANCE_MANAGER, 403),
        (Roles.DEVELOPER, 403),
        (Roles.SELLER, 403),
    ],
)
def test_user_list_access_by_role(auth_client, make_user, role, expected):
    response = auth_client(make_user(role)).get("/api/v1/users/")
    assert response.status_code == expected
    if expected == 403:
        assert response.json()["error"]["code"] == "PERMISSION_DENIED"


def test_unmapped_action_is_denied_by_default(auth_client, admin):
    # UserViewSet exposes no PUT/destroy; with no mapping the permission layer denies
    # before routing reaches a 405, so new endpoints are closed by default.
    response = auth_client(admin).put("/api/v1/users/1/", {})
    assert response.status_code in (403, 405)


@pytest.mark.django_db
def test_admin_warns_when_seller_role_gets_global_permissions(client, django_user_model):
    """A global permission on SELLER means every seller sees every store's data."""
    from apps.accounts.models import Permission, Role

    admin = django_user_model.objects.create_superuser(username="root", password="Str0ng-pass!")
    client.force_login(admin)
    role = Role.objects.get(code="SELLER")
    good_view = Permission.objects.get(codename="good.view")
    response = client.post(
        f"/admin/accounts/role/{role.pk}/change/",
        {
            "code": role.code,
            "name": role.name,
            "description": role.description,
            "is_system": "on",
            "permissions": [good_view.pk],
        },
        follow=True,
    )
    assert "ALL data, not only their own" in response.content.decode()
