"""RBAC catalog: the code-level source of truth for permissions and default roles.

`sync_rbac()` runs after every `migrate`. It:
  * creates permissions that are new in the catalog,
  * creates missing system roles with their default grants,
  * grants *newly created* permissions to the roles that list them by default.

It never revokes grants from existing roles, so role edits made in the admin survive
deploys. Permissions removed from the catalog are reported as stale and only deleted
with `manage.py sync_rbac --prune`.
"""

from dataclasses import dataclass, field

from django.db import transaction

PERMISSIONS: dict[str, str] = {
    # Users & access control
    "user.view": "View user accounts",
    "user.manage": "Create, update and deactivate user accounts",
    "role.view": "View roles and their permissions",
    "role.assign": "Assign and remove roles on users",
    "audit.view": "View the audit log",
    "stats.view": "View company statistics (people, attendance, money)",
    # Developers
    "developer.view": "View developers",
    "developer.create": "Create developers",
    "developer.update": "Update developers",
    "developer.delete": "Delete developers",
    # RFID
    "rfid.view": "View RFID cards, devices and events",
    "rfid.assign": "Assign and replace RFID cards",
    "rfid.block": "Block RFID cards",
    "rfid.device.manage": "Register and manage RFID readers",
    # Attendance
    "attendance.view": "View attendance records",
    "attendance.correct": "Correct attendance records",
    # Finance
    "finance.view": "View developer accounts and ledgers",
    "finance.deposit": "Deposit funds to developer accounts",
    "finance.adjust": "Make manual balance adjustments",
    # Seller finance
    "seller_finance.view": "View seller balances, ledgers and payouts",
    "seller_finance.payout": "Request on behalf of sellers, approve, reject and pay payouts",
    "seller_finance.adjust": "Make manual seller balance adjustments",
    # Purchases
    "purchase.view": "View purchases",
    "purchase.create": "Create purchases",
    "purchase.confirm": "Confirm purchases",
    "purchase.cancel": "Cancel purchases",
    # Sellers & goods
    "seller.view": "View sellers",
    "seller.create": "Create sellers",
    "seller.update": "Update sellers",
    "good.view": "View goods",
    "good.create": "Create goods",
    "good.update": "Update goods",
    "good.delete": "Delete goods",
    "good.stock": "Restock, write off and adjust stock",
}

ALL = frozenset(PERMISSIONS)
# Every read permission: the BOSS sees all data, changes nothing.
VIEW = frozenset(p for p in PERMISSIONS if p.endswith(".view"))


class Roles:
    ADMIN = "ADMIN"
    BOSS = "BOSS"
    MANAGER = "MANAGER"
    FINANCE_MANAGER = "FINANCE_MANAGER"
    DEVELOPER = "DEVELOPER"
    SELLER = "SELLER"
    BUILDING_MANAGER = "BUILDING_MANAGER"


@dataclass(frozen=True)
class RoleSpec:
    name: str
    description: str
    permissions: frozenset[str] = field(default_factory=frozenset)


def _prefixed(*prefixes: str) -> frozenset[str]:
    return frozenset(p for p in PERMISSIONS if p.split(".")[0] in prefixes)


# DEVELOPER and SELLER get no global permissions: their access is to their *own*
# records (own account, own goods), enforced by object-level scoping in each module.
ROLES: dict[str, RoleSpec] = {
    Roles.ADMIN: RoleSpec("Admin", "Full access: users, roles, settings and all data", ALL),
    Roles.BOSS: RoleSpec("Boss", "Sees all data and statistics, read-only", VIEW),
    Roles.MANAGER: RoleSpec(
        "Manager",
        "Manages developers, RFID and attendance",
        _prefixed("developer", "rfid", "attendance")
        | {"user.view", "role.view", "audit.view", "seller.view", "good.view"},
    ),
    Roles.FINANCE_MANAGER: RoleSpec(
        "Finance manager",
        "Manages developer balances and deposits",
        _prefixed("finance", "seller_finance") | {"developer.view", "purchase.view", "audit.view"},
    ),
    # Every permission of this role is narrowed to the user's buildings
    # (Building.managers); see apps/rfid/scope.py.
    Roles.BUILDING_MANAGER: RoleSpec(
        "Building manager",
        "Developers, cards, doors, attendance and sales of their own building",
        {
            "developer.view",
            "developer.create",
            "developer.update",
            "rfid.view",
            "rfid.assign",
            "rfid.block",
            "rfid.device.manage",
            "attendance.view",
            "attendance.correct",
            "purchase.view",
        },
    ),
    Roles.DEVELOPER: RoleSpec("Developer", "Self-service access to own data"),
    Roles.SELLER: RoleSpec("Seller", "Self-service access to own goods and sales"),
}

for _code, _spec in ROLES.items():
    _unknown = _spec.permissions - ALL
    assert not _unknown, f"Role {_code} references unknown permissions: {_unknown}"


@dataclass
class SyncResult:
    created_permissions: list[str]
    created_roles: list[str]
    stale_permissions: list[str]


@transaction.atomic
def sync_rbac(prune: bool = False) -> SyncResult:
    from .models import Permission, Role

    existing = {p.codename: p for p in Permission.objects.all()}
    created_perms = []
    for codename, description in PERMISSIONS.items():
        perm = existing.get(codename)
        if perm is None:
            existing[codename] = Permission.objects.create(
                codename=codename, description=description
            )
            created_perms.append(codename)
        elif perm.description != description:
            perm.description = description
            perm.save(update_fields=["description"])

    created_roles = []
    for code, spec in ROLES.items():
        role, created = Role.objects.get_or_create(
            code=code,
            defaults={"name": spec.name, "description": spec.description, "is_system": True},
        )
        grant = spec.permissions if created else spec.permissions & set(created_perms)
        if grant:
            role.permissions.add(*(existing[c] for c in grant))
        if created:
            created_roles.append(code)

    stale = sorted(set(existing) - ALL)
    if prune and stale:
        Permission.objects.filter(codename__in=stale).delete()

    return SyncResult(sorted(created_perms), created_roles, stale)


def sync_rbac_after_migrate(sender, **kwargs):
    sync_rbac()
