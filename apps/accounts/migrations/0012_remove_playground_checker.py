from django.db import migrations


def remove(apps, schema_editor):
    """Bookings are no longer checked in: the playground checker role and its permission
    go. Users who had it keep their other roles (or none)."""
    Role = apps.get_model("accounts", "Role")
    Permission = apps.get_model("accounts", "Permission")
    UserRole = apps.get_model("accounts", "UserRole")
    UserRole.objects.filter(role__code="PLAYGROUND_CHECKER").delete()
    Role.objects.filter(code="PLAYGROUND_CHECKER").delete()
    Permission.objects.filter(codename="booking.check").delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0011_feature_permissions")]

    operations = [migrations.RunPython(remove, migrations.RunPython.noop)]
