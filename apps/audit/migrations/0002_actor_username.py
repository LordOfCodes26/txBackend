"""The actor is recorded by username (users have no email any more). Older entries keep the
actor's email address in this column."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("audit", "0001_initial")]

    operations = [
        migrations.RenameField(model_name="auditlog", old_name="actor_email", new_name="actor_username"),
    ]
