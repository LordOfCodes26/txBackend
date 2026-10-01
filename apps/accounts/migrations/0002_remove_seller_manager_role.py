from django.db import migrations


def remove_seller_manager(apps, schema_editor):
    """SELLER_MANAGER was dropped from the catalog: a store is managed by its seller login
    (and position managers); BOSS manages all stores. Delete the role if nobody has it;
    otherwise keep it as an ordinary custom role, so nobody loses access silently."""
    Role = apps.get_model("accounts", "Role")
    role = Role.objects.filter(code="SELLER_MANAGER").first()
    if role is None:
        return
    if role.user_roles.exists():
        role.is_system = False
        role.save(update_fields=["is_system"])
    else:
        role.delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0001_initial")]

    operations = [migrations.RunPython(remove_seller_manager, migrations.RunPython.noop)]
