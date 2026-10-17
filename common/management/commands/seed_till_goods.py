import random
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.goods import services as goods
from apps.goods.models import Good, GoodKind
from apps.sellers.models import ServicePosition

SKU = "SEED-TILL-"  # each seeded good: SEED-TILL-<counter id>-<number>
KINDS = [
    "Coffee",
    "Tea",
    "Juice",
    "Water",
    "Sandwich",
    "Cake",
    "Cookie",
    "Salad",
    "Soup",
    "Noodles",
]
STYLES = [
    "Small",
    "Large",
    "Hot",
    "Cold",
    "Classic",
    "Special",
    "Fresh",
    "Double",
    "Mini",
    "Family",
]


class Command(BaseCommand):
    help = (
        "Test data: adds many goods to one till counter (e.g. 120 on counter 1) so the till's "
        "search and pages can be tried. Running it again adds only the missing ones; --undo "
        "removes them (soft delete). Test servers only (DEBUG or TEST_CONSOLE_ENABLED)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--counter", type=int, required=True, help="Service position id.")
        parser.add_argument("--count", type=int, default=120, help="How many goods.")
        parser.add_argument("--undo", action="store_true", help="Remove what this seeded.")

    def handle(self, *args, counter, count=120, undo=False, **options):
        if not (settings.DEBUG or settings.TEST_CONSOLE_ENABLED):
            raise CommandError(
                "seed_till_goods only runs on a test server (DEBUG or TEST_CONSOLE_ENABLED)."
            )
        position = ServicePosition.objects.filter(pk=counter).select_related("seller").first()
        if position is None:
            raise CommandError(f"No counter {counter}.")
        prefix = f"{SKU}{position.pk}-"
        with transaction.atomic():
            if undo:
                seeded = Good.objects.filter(sku__startswith=prefix)
                for good in seeded:
                    goods.delete_good(actor=None, good=good)
                self.stdout.write(self.style.SUCCESS(f"Removed {len(seeded)} seeded goods."))
                return
            if count < 1:
                raise CommandError("--count must be more than 0.")
            have = set(Good.objects.filter(sku__startswith=prefix).values_list("sku", flat=True))
            rng = random.Random(position.pk)  # the same names and prices every time
            added = 0
            for number in range(1, count + 1):
                sku = f"{prefix}{number:04d}"
                kind, style = KINDS[(number - 1) % len(KINDS)], rng.choice(STYLES)
                price = Decimal(rng.randrange(50, 1500, 25)) / 100
                if sku in have:
                    continue
                stocked = number % 3 == 0
                goods.create_good(
                    actor=None,
                    service_position=position,
                    name=f"{style} {kind} {number}",
                    sku=sku,
                    price=price,
                    kind=GoodKind.PRODUCT,
                    track_stock=stocked,
                    initial_quantity=rng.randrange(5, 50) if stocked else 0,
                )
                added += 1
        self.stdout.write(
            self.style.SUCCESS(
                f"{position.seller.name} / {position.name}: added {added} goods "
                f"({count - added} were there already)."
            )
        )
