"""Seller finance (seller balances, sale credits, payouts) is removed: drop its tables,
its migration history, content types and permissions. Purchases stay; they only charge
the developer. Safe on new databases, where the tables never existed."""

from django.db import migrations

TABLES = (
    "seller_finance_sellerpayment",
    "seller_finance_sellertransaction",
    "seller_finance_selleraccount",
)


def forwards(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"DROP TABLE IF EXISTS {', '.join(TABLES)} CASCADE")
        cursor.execute("DELETE FROM django_migrations WHERE app = 'seller_finance'")
    # Django's own permissions go with their content type (CASCADE); admin log entries
    # keep their text.
    apps.get_model("contenttypes", "ContentType").objects.filter(
        app_label="seller_finance"
    ).delete()
    # The RBAC permissions seller_finance.* and their grants on roles.
    apps.get_model("accounts", "Permission").objects.filter(
        codename__startswith="seller_finance."
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("sellers", "0002_position_building_manager"),
        ("accounts", "0004_username_instead_of_email"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("admin", "0003_logentry_add_action_flag_choices"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
