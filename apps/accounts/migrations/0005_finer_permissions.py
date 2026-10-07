"""Finer permissions. Every role (system or made by an admin) keeps what it could do:

  rfid.view           -> card.view, reader.view, scan.view
  rfid.assign         -> card.register, card.assign
  rfid.block          -> card.block
  rfid.device.manage  -> reader.manage, building.manage
  purchase.view       -> booking.view            (the playground desk used purchase rights)
  purchase.create     -> booking.create, booking.change
  purchase.cancel     -> booking.cancel
  seller.update       -> counter.manage
  good.create         -> court.manage
  any *.view          -> excel.export            (downloads used the view rights alone)
  developer.create, card.assign or finance.deposit -> excel.import
  role.assign on ADMIN -> role.manage

The rfid.* permissions are then deleted. Descriptions are filled in by sync_rbac after
migrate.
"""

from django.db import migrations

SPLIT = {
    "rfid.view": ["card.view", "reader.view", "scan.view"],
    "rfid.assign": ["card.register", "card.assign"],
    "rfid.block": ["card.block"],
    "rfid.device.manage": ["reader.manage", "building.manage"],
    "purchase.view": ["booking.view"],
    "purchase.create": ["booking.create", "booking.change"],
    "purchase.cancel": ["booking.cancel"],
    "seller.update": ["counter.manage"],
    "good.create": ["court.manage"],
}
OLD = ["rfid.view", "rfid.assign", "rfid.block", "rfid.device.manage"]
IMPORT_FROM = {"developer.create", "rfid.assign", "finance.deposit"}


def forward(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Role = apps.get_model("accounts", "Role")

    def perm(codename):
        return Permission.objects.get_or_create(codename=codename)[0]

    for role in Role.objects.prefetch_related("permissions"):
        had = set(role.permissions.values_list("codename", flat=True))
        new = set()
        for old, replacements in SPLIT.items():
            if old in had:
                new.update(replacements)
        if any(code.endswith(".view") for code in had):
            new.add("excel.export")
        if had & IMPORT_FROM:
            new.add("excel.import")
        if role.code == "ADMIN":
            new.add("role.manage")
        role.permissions.add(*(perm(code) for code in sorted(new)))
    Permission.objects.filter(codename__in=OLD).delete()


def backward(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Role = apps.get_model("accounts", "Role")
    back = {
        "card.view": "rfid.view",
        "reader.view": "rfid.view",
        "scan.view": "rfid.view",
        "card.register": "rfid.assign",
        "card.assign": "rfid.assign",
        "card.block": "rfid.block",
        "reader.manage": "rfid.device.manage",
        "building.manage": "rfid.device.manage",
    }
    for role in Role.objects.prefetch_related("permissions"):
        had = set(role.permissions.values_list("codename", flat=True))
        old = {back[code] for code in had if code in back}
        role.permissions.add(*(Permission.objects.get_or_create(codename=c)[0] for c in old))
    new_only = {r for rs in SPLIT.values() for r in rs} | {
        "excel.export",
        "excel.import",
        "role.manage",
    }
    Permission.objects.filter(codename__in=new_only).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0004_username_instead_of_email")]

    operations = [migrations.RunPython(forward, backward)]
