"""Finance deletes cash sales itself (no requests to approve): `cash.approve` becomes
`cash.delete`, for every role that had it. Its description is filled in by sync_rbac."""

from django.db import migrations


def rename(old, new):
    def run(apps, schema_editor):
        Permission = apps.get_model("accounts", "Permission")
        Permission.objects.filter(codename=old).update(codename=new)

    return run


class Migration(migrations.Migration):
    dependencies = [("accounts", "0012_remove_playground_checker")]

    operations = [
        migrations.RunPython(
            rename("cash.approve", "cash.delete"), rename("cash.delete", "cash.approve")
        )
    ]
