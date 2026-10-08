from django.db import migrations

OLD = "Own store's courts, court bookings and playground desk"
NEW = "Every court, court booking and the playground desk (shared, no store needed)"


def describe(apps, schema_editor):
    """The playground is shared now: say so, unless an admin changed the text."""
    Role = apps.get_model("accounts", "Role")
    Role.objects.filter(code="PLAYGROUND_SELLER", description=OLD).update(description=NEW)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0008_split_seller_role")]

    operations = [migrations.RunPython(describe, migrations.RunPython.noop)]
