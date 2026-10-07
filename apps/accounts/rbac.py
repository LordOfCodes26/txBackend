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

# Every permission, by area, with what it allows. The areas order the role pages.
AREAS: dict[str, dict[str, str]] = {
    "Users and access": {
        "user.view": "See user accounts",
        "user.manage": "Create, change and deactivate user accounts",
        "role.view": "See roles and what each one allows",
        "role.assign": "Give roles to users and take them away",
        "role.manage": "Create roles and change what each role allows",
        "audit.view": "Read the audit log",
        "stats.view": "See company statistics (people, attendance, money)",
    },
    "Developers": {
        "developer.view": "See developers and their profiles",
        "developer.create": "Add developers",
        "developer.update": "Change developers' profiles and status",
        "developer.delete": "Delete developers (soft delete)",
    },
    "Cards": {
        "card.view": "See RFID cards and who held them",
        "card.register": "Register new cards and change their labels",
        "card.assign": "Assign, replace, unassign and retire cards",
        "card.block": "Block and unblock cards",
    },
    "Readers and buildings": {
        "reader.view": "See door units, till readers and card assign readers",
        "reader.manage": "Register readers, change them, renew their keys and assign till readers",
        "building.manage": "Add, rename and delete buildings",
        "scan.view": "See card scans at doors and tills",
    },
    "Attendance": {
        "attendance.view": "See who is inside, attendance and statistics",
        "attendance.correct": "Add manual attendance records and void wrong ones",
    },
    "Money": {
        "finance.view": "See wallets, transactions and finance statistics",
        "finance.deposit": "Deposit money into wallets",
        "finance.adjust": "Make manual balance corrections",
    },
    "Sales": {
        "purchase.view": "See purchases and sales statistics",
        "purchase.create": "Open a till and build a purchase",
        "purchase.confirm": "Take payment for a purchase (card and PIN)",
        "purchase.cancel": "Cancel a purchase that is not paid yet",
    },
    "Playground": {
        "booking.view": "See court bookings and the playground desk",
        "booking.create": "Book a court at the desk (card and PIN)",
        "booking.change": "Move a booking to another time or court",
        "booking.cancel": "Cancel a court checkout that is not paid yet",
        "court.manage": "Add courts and change their prices and rules",
    },
    "Stores and goods": {
        "seller.view": "See stores and their counters",
        "seller.create": "Add stores",
        "seller.update": "Change stores",
        "counter.manage": "Add, change and delete stores' counters",
        "good.view": "See goods and stock movements",
        "good.create": "Add goods",
        "good.update": "Change goods, prices and photos",
        "good.delete": "Delete goods (soft delete)",
        "good.stock": "Restock, write off and count stock",
    },
    "Excel": {
        "excel.export": "Download lists as Excel (only data you can see)",
        "excel.import": "Import developers, cards and opening balances from Excel",
    },
    "System": {
        "system.tcp_log": "Read the raw TCP device log (every packet and its answer)",
        "system.backup": "See backups, run one now, change backup settings, restore and download",
        "system.delete_records": "Delete a record for good, with its history (after a backup)",
        "system.data_reset": (
            "Delete all data except users, roles, readers and stores (after a backup)"
        ),
    },
}

PERMISSIONS: dict[str, str] = {
    codename: description for area in AREAS.values() for codename, description in area.items()
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
    BUILDING_OWNER = "BUILDING_OWNER"


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
    Roles.BOSS: RoleSpec(
        "Boss", "Sees all data and statistics, read-only", VIEW | {"excel.export"}
    ),
    Roles.MANAGER: RoleSpec(
        "Manager",
        "Manages developers, cards, readers and attendance",
        _prefixed("developer", "card", "reader", "building", "scan", "attendance")
        | {"user.view", "role.view", "audit.view", "seller.view", "good.view"}
        | {"excel.export", "excel.import"},
    ),
    Roles.FINANCE_MANAGER: RoleSpec(
        "Finance manager",
        "Manages developer balances and deposits",
        _prefixed("finance")
        | {"developer.view", "purchase.view", "booking.view", "audit.view"}
        | {"excel.export", "excel.import"},
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
            "card.view",
            "card.register",
            "card.assign",
            "card.block",
            "reader.view",
            "reader.manage",
            "building.manage",
            "scan.view",
            "attendance.view",
            "attendance.correct",
            "purchase.view",
            "booking.view",
            "excel.export",
            "excel.import",
        },
    ),
    # Like BOSS (read-only + statistics) but narrowed to the user's buildings
    # (Building.owners), including the stores there. Users, roles and the audit log span
    # all buildings, so they are left out.
    Roles.BUILDING_OWNER: RoleSpec(
        "Building owner",
        "Sees the data, stores and statistics of their own buildings, read-only",
        (VIEW - {"user.view", "role.view", "audit.view"}) | {"excel.export"},
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
