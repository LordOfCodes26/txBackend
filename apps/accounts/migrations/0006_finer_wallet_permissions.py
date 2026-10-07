"""Finer wallet permissions. Every role keeps what it could do:

  finance.view     -> finance.stats.view      (the statistics page and money reports)
  finance.deposit  -> finance.pin_change      (the PIN desk changed PINs with deposit rights)
  finance.adjust   -> finance.pin_reset, finance.freeze, finance.close

Descriptions are filled in by sync_rbac after migrate.
"""

from django.db import migrations

SPLIT = {
    "finance.view": ["finance.stats.view"],
    "finance.deposit": ["finance.pin_change"],
    "finance.adjust": ["finance.pin_reset", "finance.freeze", "finance.close"],
}
NEW = sorted({code for codes in SPLIT.values() for code in codes})


def forward(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Role = apps.get_model("accounts", "Role")
    for role in Role.objects.prefetch_related("permissions"):
        had = set(role.permissions.values_list("codename", flat=True))
        new = {code for old, codes in SPLIT.items() if old in had for code in codes}
        role.permissions.add(*(Permission.objects.get_or_create(codename=c)[0] for c in sorted(new)))


def backward(apps, schema_editor):
    apps.get_model("accounts", "Permission").objects.filter(codename__in=NEW).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0005_finer_permissions")]

    operations = [migrations.RunPython(forward, backward)]
