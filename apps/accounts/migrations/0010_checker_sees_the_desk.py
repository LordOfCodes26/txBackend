from django.db import migrations

OLD = "Checks with a card tap that developers at the playground booked it"
NEW = "Checks with a card tap that developers at the playground booked it; sees the desk"


def grant(apps, schema_editor):
    """Playground checkers see the playground desk (bookings), read-only."""
    Role = apps.get_model("accounts", "Role")
    Permission = apps.get_model("accounts", "Permission")
    role = Role.objects.filter(code="PLAYGROUND_CHECKER").first()
    view = Permission.objects.filter(codename="booking.view").first()
    if role is None or view is None:
        return  # a new install: sync_rbac creates the role with it
    role.permissions.add(view)
    if role.description == OLD:
        role.description = NEW
        role.save(update_fields=["description"])


class Migration(migrations.Migration):
    dependencies = [("accounts", "0009_shared_playground_role_description")]

    operations = [migrations.RunPython(grant, migrations.RunPython.noop)]
