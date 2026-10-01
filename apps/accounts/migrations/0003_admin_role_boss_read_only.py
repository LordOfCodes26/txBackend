from django.db import migrations


def split_admin_from_boss(apps, schema_editor):
    """ADMIN takes over full access; BOSS becomes read-only (every *.view permission).

    Everyone who had BOSS also gets ADMIN, so nobody loses access; remove ADMIN from the
    users who should only look at the data. On a fresh database the roles don't exist yet:
    sync_rbac (after migrate) creates them from the catalog.
    """
    Permission = apps.get_model("accounts", "Permission")
    Role = apps.get_model("accounts", "Role")
    UserRole = apps.get_model("accounts", "UserRole")

    boss = Role.objects.filter(code="BOSS").first()
    if boss is None:
        return
    Permission.objects.get_or_create(
        codename="stats.view",
        defaults={"description": "View company statistics (people, attendance, money)"},
    )
    admin, _ = Role.objects.get_or_create(
        code="ADMIN",
        defaults={
            "name": "Admin",
            "description": "Full access: users, roles, settings and all data",
            "is_system": True,
        },
    )
    admin.permissions.set(Permission.objects.all())
    for user_role in UserRole.objects.filter(role=boss):
        UserRole.objects.get_or_create(user_id=user_role.user_id, role=admin)
    boss.description = "Sees all data and statistics, read-only"
    boss.save(update_fields=["description"])
    boss.permissions.set(Permission.objects.filter(codename__endswith=".view"))


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_remove_seller_manager_role")]

    operations = [migrations.RunPython(split_admin_from_boss, migrations.RunPython.noop)]
