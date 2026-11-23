"""The sellers keep their cash (finance pays them their card sales), so nothing is handed over:
`cash.handover` goes, from every role."""

from django.db import migrations


def forward(apps, schema_editor):
    apps.get_model("accounts", "Permission").objects.filter(codename="cash.handover").delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0014_no_cash_count")]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
