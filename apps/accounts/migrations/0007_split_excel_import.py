"""excel.import becomes one permission per import. A role keeps the imports it could
run: excel.import together with the underlying right.

  developer.create or developer.update -> excel.import_developers
  card.assign                          -> excel.import_cards
  finance.deposit                      -> excel.import_balances

excel.import is then deleted. Descriptions are filled in by sync_rbac after migrate.
"""

from django.db import migrations

NEEDS = {
    "excel.import_developers": {"developer.create", "developer.update"},
    "excel.import_cards": {"card.assign"},
    "excel.import_balances": {"finance.deposit"},
}


def forward(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Role = apps.get_model("accounts", "Role")
    for role in Role.objects.prefetch_related("permissions"):
        had = set(role.permissions.values_list("codename", flat=True))
        if "excel.import" not in had:
            continue
        new = [code for code, needs in NEEDS.items() if had & needs]
        role.permissions.add(*(Permission.objects.get_or_create(codename=c)[0] for c in new))
    Permission.objects.filter(codename="excel.import").delete()


def backward(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Role = apps.get_model("accounts", "Role")
    old = Permission.objects.get_or_create(codename="excel.import")[0]
    for role in Role.objects.filter(permissions__codename__in=list(NEEDS)).distinct():
        role.permissions.add(old)
    Permission.objects.filter(codename__in=list(NEEDS)).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0006_finer_wallet_permissions")]

    operations = [migrations.RunPython(forward, backward)]
