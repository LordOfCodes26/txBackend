"""Users sign in with a username; the email address is removed.

Existing users get the part of their email before the @ (cleaned to letters, digits,
dot, underscore, hyphen; lower-case), numbered when two would clash: admin@a.com and
admin@b.com become admin and admin2.
"""

import re

import django.core.validators
import django.db.models.functions.text
from django.db import migrations, models


def usernames_from_emails(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    taken = set()
    for user in User.objects.order_by("date_joined", "pk"):
        base = re.sub(r"[^a-z0-9._-]", "", (user.email or "").split("@")[0].lower())
        base = (base or "user")[:140]
        if len(base) < 3:
            base = f"{base}user"[:140]
        name, n = base, 1
        while name in taken:
            n += 1
            name = f"{base}{n}"
        taken.add(name)
        user.username = name
        user.save(update_fields=["username"])


class Migration(migrations.Migration):
    dependencies = [("accounts", "0003_admin_role_boss_read_only")]

    operations = [
        migrations.AddField(
            model_name="user",
            name="username",
            field=models.CharField(max_length=150, null=True),
        ),
        migrations.RunPython(usernames_from_emails, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="user",
            name="username",
            field=models.CharField(
                max_length=150,
                unique=True,
                validators=[
                    django.core.validators.RegexValidator(
                        "^[A-Za-z0-9._-]{3,150}$",
                        "3 to 150 letters, digits, dots, underscores or hyphens.",
                    )
                ],
            ),
        ),
        migrations.RemoveConstraint(model_name="user", name="accounts_user_email_ci_unique"),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower("username"),
                name="accounts_user_username_ci_unique",
            ),
        ),
        migrations.RemoveField(model_name="user", name="email"),
    ]
