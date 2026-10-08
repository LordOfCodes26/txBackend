from django.db import migrations

NEW_ROLES = {
    "PLAYGROUND_SELLER": (
        "Playground seller",
        "Own store's courts, court bookings and playground desk",
    ),
    "GOODS_SELLER": ("Goods seller", "Own store's goods, stock, till and sales"),
}


def split_seller(apps, schema_editor):
    """SELLER became two roles, one per part of a store. Every SELLER user gets both, so
    nobody loses access; then SELLER is removed."""
    Role = apps.get_model("accounts", "Role")
    UserRole = apps.get_model("accounts", "UserRole")
    new = []
    for code, (name, description) in NEW_ROLES.items():
        role, _ = Role.objects.get_or_create(
            code=code, defaults={"name": name, "description": description, "is_system": True}
        )
        new.append(role)
    old = Role.objects.filter(code="SELLER").first()
    if old is None:
        return
    for link in UserRole.objects.filter(role=old):
        for role in new:
            UserRole.objects.get_or_create(
                user_id=link.user_id, role=role, defaults={"assigned_by_id": link.assigned_by_id}
            )
    UserRole.objects.filter(role=old).delete()
    old.delete()


def merge_sellers(apps, schema_editor):
    """Back: SELLER again, for everyone with either seller role."""
    Role = apps.get_model("accounts", "Role")
    UserRole = apps.get_model("accounts", "UserRole")
    seller, _ = Role.objects.get_or_create(
        code="SELLER",
        defaults={
            "name": "Seller",
            "description": "Self-service access to own goods and sales",
            "is_system": True,
        },
    )
    for link in UserRole.objects.filter(role__code__in=NEW_ROLES):
        UserRole.objects.get_or_create(user_id=link.user_id, role=seller)
    UserRole.objects.filter(role__code__in=NEW_ROLES).delete()
    Role.objects.filter(code__in=NEW_ROLES).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0007_split_excel_import")]

    operations = [migrations.RunPython(split_seller, merge_sellers)]
