"""Finance no longer counts the stores' stock: `cash.count` goes, from every role."""

from django.db import migrations


def forward(apps, schema_editor):
    apps.get_model("accounts", "Permission").objects.filter(codename="cash.count").delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0013_cash_delete")]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
