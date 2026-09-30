import pytest

from apps.accounts.models import Role, User, UserRole
from apps.accounts.rbac import Roles
from apps.audit.models import AuditLog

pytestmark = pytest.mark.django_db


def assign(client, user, role):
    return client.post(f"/api/v1/users/{user.pk}/roles/", {"role": role})


def test_boss_assigns_and_removes_role_with_audit(auth_client, boss, make_user):
    dev = make_user()
    client = auth_client(boss)

    response = assign(client, dev, Roles.MANAGER)
    assert response.status_code == 201
    assert response.json()["roles"] == [Roles.MANAGER]

    assert client.delete(f"/api/v1/users/{dev.pk}/roles/MANAGER/").status_code == 204
    assert not dev.roles.exists()

    actions = list(
        AuditLog.objects.filter(entity_id=str(dev.pk)).order_by("id").values_list("action", "actor")
    )
    assert actions == [("user.role_assigned", boss.pk), ("user.role_removed", boss.pk)]


def test_duplicate_assignment_conflicts(auth_client, boss, make_user):
    dev = make_user(Roles.DEVELOPER)
    response = assign(auth_client(boss), dev, Roles.DEVELOPER)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ROLE_ALREADY_ASSIGNED"


def test_removing_unassigned_role_is_404(auth_client, boss, make_user):
    dev = make_user()
    response = auth_client(boss).delete(f"/api/v1/users/{dev.pk}/roles/SELLER/")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ROLE_NOT_ASSIGNED"


@pytest.fixture
def role_admin(make_user):
    """A non-boss who may assign roles but only holds MANAGER-level permissions."""
    user = make_user(Roles.MANAGER)
    custom = Role.objects.create(code="ROLE_ADMIN", name="Role admin")
    custom.permissions.set(
        Role.objects.get(code=Roles.MANAGER)
        .permissions.all()
        .union(Role.objects.get(code=Roles.BOSS).permissions.filter(codename="role.assign"))
    )
    UserRole.objects.create(user=user, role=custom)
    return user


def test_cannot_grant_role_with_permissions_you_lack(auth_client, role_admin, make_user):
    dev = make_user()
    client = auth_client(role_admin)

    assert assign(client, dev, Roles.DEVELOPER).status_code == 201
    response = assign(client, dev, Roles.BOSS)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PRIVILEGE_ESCALATION"
    # Not even to themselves.
    assert assign(client, role_admin, Roles.FINANCE_MANAGER).status_code == 403


def test_cannot_manage_more_privileged_user(auth_client, role_admin, boss):
    client = auth_client(role_admin)
    assert client.delete(f"/api/v1/users/{boss.pk}/roles/BOSS/").status_code == 403


def test_user_manager_cannot_reactivate_deactivated_boss(auth_client, make_user, boss):
    other_boss = make_user(Roles.BOSS, is_active=False)
    manager = make_user(Roles.MANAGER)
    custom = Role.objects.create(code="USER_ADMIN", name="User admin")
    custom.permissions.set(
        Role.objects.get(code=Roles.BOSS).permissions.filter(
            codename__in=["user.manage", "user.view"]
        )
    )
    UserRole.objects.create(user=manager, role=custom)

    response = auth_client(manager).patch(f"/api/v1/users/{other_boss.pk}/", {"is_active": True})
    assert response.status_code == 403
    other_boss.refresh_from_db()
    assert other_boss.is_active is False


def test_last_boss_cannot_lose_boss_role_or_be_deactivated(auth_client, boss):
    client = auth_client(boss)

    response = client.delete(f"/api/v1/users/{boss.pk}/roles/BOSS/")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LAST_BOSS"

    response = client.patch(f"/api/v1/users/{boss.pk}/", {"is_active": False})
    assert response.status_code == 409
    assert User.objects.get(pk=boss.pk).is_active


def test_boss_can_step_down_when_another_boss_exists(auth_client, boss, make_user):
    make_user(Roles.BOSS)
    assert auth_client(boss).delete(f"/api/v1/users/{boss.pk}/roles/BOSS/").status_code == 204


def test_roles_endpoint_lists_permissions(auth_client, boss):
    response = auth_client(boss).get("/api/v1/roles/SELLER_MANAGER/")
    assert response.status_code == 200
    assert "good.create" in response.json()["permissions"]
