"""Own permissions for newer features. Every role (system or made by an admin) keeps what
it could do: the new permission goes to each role that has the one it used to need.

  good.update      -> good.category          (goods categories)
  finance.view     -> finance.deposits.view  (the deposit list)
  purchase.view    -> purchase.report.view   (the sales report)
  booking.create   -> booking.pay            (taking booking payments)

Descriptions are filled in by sync_rbac after migrate.
"""

from django.db import migrations

FROM = {
    "good.update": "good.category",
    "finance.view": "finance.deposits.view",
    "purchase.view": "purchase.report.view",
    "booking.create": "booking.pay",
}


def forward(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Role = apps.get_model("accounts", "Role")
    new = {code: Permission.objects.get_or_create(codename=code)[0] for code in FROM.values()}
    for role in Role.objects.prefetch_related("permissions"):
        had = set(role.permissions.values_list("codename", flat=True))
        role.permissions.add(*(new[target] for base, target in FROM.items() if base in had))


def backward(apps, schema_editor):
    apps.get_model("accounts", "Permission").objects.filter(codename__in=FROM.values()).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0010_checker_sees_the_desk")]

    operations = [migrations.RunPython(forward, backward)]
