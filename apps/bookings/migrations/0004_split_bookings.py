from django.db import migrations, models
from django.db.models import Q


def mark_booking_purchases(apps, schema_editor):
    """Purchases that paid for a booking (or hold a court line) become BOOKING checkouts."""
    Purchase = apps.get_model("purchases", "Purchase")
    Purchase.objects.filter(Q(bookings__isnull=False) | Q(items__start__isnull=False)).update(
        kind="BOOKING"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("bookings", "0003_booking_at_desk"),
        ("purchases", "0006_split_bookings"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="change_count",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.RunPython(mark_booking_purchases, migrations.RunPython.noop),
    ]
