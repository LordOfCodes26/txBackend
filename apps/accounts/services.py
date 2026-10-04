from django.db import transaction
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

from apps.audit.services import record_audit

from .exceptions import LastAdmin, PrivilegeEscalation, RoleAlreadyAssigned, RoleNotAssigned
from .models import Permission, Role, User, UserRole
from .rbac import Roles


def granted_permissions(user: User) -> frozenset[str]:
    """Permissions a user holds through roles, regardless of `is_active`."""
    qs = Permission.objects.all()
    if not user.is_superuser:
        qs = qs.filter(roles__user_roles__user=user).distinct()
    return frozenset(qs.values_list("codename", flat=True))


def _ensure_can_manage(actor: User, permissions: frozenset[str]) -> None:
    """An actor may only act on users or roles whose permissions they fully hold."""
    if actor.is_superuser:
        return
    if not permissions <= actor.rbac_permissions:
        raise PrivilegeEscalation()


def _ensure_other_admin_remains(user: User) -> None:
    # Lock the ADMIN role row so concurrent removals serialize on this check.
    admin = Role.objects.select_for_update().filter(code=Roles.ADMIN).first()
    if admin is None or not UserRole.objects.filter(user=user, role=admin).exists():
        return
    others = UserRole.objects.filter(role=admin, user__is_active=True).exclude(user=user).exists()
    if not others:
        raise LastAdmin()


def _user_snapshot(user: User) -> dict:
    return {"username": user.username, "full_name": user.full_name, "is_active": user.is_active}


@transaction.atomic
def create_user(*, actor: User, username: str, password: str, full_name: str = "") -> User:
    user = User.objects.create_user(username=username, password=password, full_name=full_name)
    record_audit("user.created", actor=actor, entity=user, new_values=_user_snapshot(user))
    return user


@transaction.atomic
def update_user(*, actor: User, user: User, **changes) -> User:
    user = User.objects.select_for_update().get(pk=user.pk)
    _ensure_can_manage(actor, granted_permissions(user))
    if changes.get("is_active") is False and user.is_active:
        _ensure_other_admin_remains(user)

    before = _user_snapshot(user)
    for field, value in changes.items():
        setattr(user, field, value)
    user.save(update_fields=list(changes))
    after = _user_snapshot(user)

    old = {k: v for k, v in before.items() if after[k] != v}
    if old:
        new = {k: after[k] for k in old}
        record_audit("user.updated", actor=actor, entity=user, old_values=old, new_values=new)
    if changes.get("is_active") is False:
        revoke_refresh_tokens(user)
    return user


@transaction.atomic
def assign_role(*, actor: User, user: User, role: Role) -> UserRole:
    role_perms = frozenset(role.permissions.values_list("codename", flat=True))
    _ensure_can_manage(actor, role_perms | granted_permissions(user))
    if UserRole.objects.filter(user=user, role=role).exists():
        raise RoleAlreadyAssigned()
    user_role = UserRole.objects.create(user=user, role=role, assigned_by=actor)
    record_audit("user.role_assigned", actor=actor, entity=user, new_values={"role": role.code})
    return user_role


@transaction.atomic
def remove_role(*, actor: User, user: User, role: Role) -> None:
    _ensure_can_manage(actor, granted_permissions(user))
    if role.code == Roles.ADMIN:
        _ensure_other_admin_remains(user)
    deleted, _ = UserRole.objects.filter(user=user, role=role).delete()
    if not deleted:
        raise RoleNotAssigned()
    record_audit("user.role_removed", actor=actor, entity=user, old_values={"role": role.code})


@transaction.atomic
def change_password(*, user: User, new_password: str) -> None:
    user.set_password(new_password)
    user.save(update_fields=["password"])
    revoke_refresh_tokens(user)
    record_audit("user.password_changed", actor=user, entity=user)


def revoke_refresh_tokens(user: User) -> None:
    """Blacklist every outstanding refresh token. Access tokens expire on their own."""
    for token in OutstandingToken.objects.filter(user=user).exclude(blacklistedtoken__isnull=False):
        BlacklistedToken.objects.get_or_create(token=token)
