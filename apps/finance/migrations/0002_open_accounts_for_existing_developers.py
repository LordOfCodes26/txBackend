from django.db import migrations


def open_accounts(apps, schema_editor):
    Developer = apps.get_model("developers", "Developer")
    DeveloperAccount = apps.get_model("finance", "DeveloperAccount")
    existing = set(DeveloperAccount.objects.values_list("developer_id", flat=True))
    DeveloperAccount.objects.bulk_create(
        DeveloperAccount(developer_id=pk)
        for pk in Developer.objects.values_list("pk", flat=True)
        if pk not in existing
    )


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0001_initial"),
        ("developers", "0001_initial"),
    ]

    operations = [migrations.RunPython(open_accounts, migrations.RunPython.noop)]
