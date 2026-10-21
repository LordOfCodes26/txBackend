from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.developers.models import Developer, DeveloperStatus
from apps.goods.models import Good, GoodKind
from apps.purchases import preferential
from apps.purchases.models import PreferentialGood, PreferentialRule, PreferentialSettings
from apps.rfid.models import RFIDCardAssignment


def _levels(text: str) -> list[dict]:
    """ "50:1,100:3" -> [{"threshold": "50", "items": 1}, ...]."""
    try:
        pairs = [part.split(":") for part in text.split(",") if part.strip()]
        return [{"threshold": str(Decimal(a)), "items": int(b)} for a, b in pairs]
    except (ValueError, ArithmeticError) as exc:
        raise CommandError("--levels: amount:items pairs, e.g. 50:1,100:3,200:5") from exc


class Command(BaseCommand):
    help = (
        "Test data for the preferential service: turns it on with sample rules (usage last "
        "month -> free items), picks the cheapest goods and the cheapest court, and lists "
        "developers who reached each rule (with their card) to try at the till. Uses the "
        "spending already in the ledger (e.g. from seed_purchases_month). Test servers only "
        "(DEBUG or TEST_CONSOLE_ENABLED)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--levels",
            default="50:1,100:3,200:5",
            help="amount:items rules (default 50:1,100:3,200:5).",
        )
        parser.add_argument("--goods", type=int, default=5, help="How many cheapest goods to pick.")
        parser.add_argument(
            "--undo",
            action="store_true",
            help="Turn the service off and clear its rules, goods and history.",
        )

    def handle(self, *args, levels="50:1,100:3,200:5", goods=5, undo=False, **options):
        if not (settings.DEBUG or settings.TEST_CONSOLE_ENABLED):
            raise CommandError(
                "seed_preferential only runs on a test server (DEBUG or TEST_CONSOLE_ENABLED)."
            )
        if undo:
            with transaction.atomic():
                PreferentialRule.objects.all().delete()
                PreferentialGood.objects.all().delete()
                PreferentialSettings.objects.update_or_create(
                    pk=1, defaults={"enabled": False, "levels": []}
                )
            self.stdout.write(self.style.SUCCESS("Preferential service off; rules cleared."))
            return

        rules = _levels(levels)
        picked = list(
            Good.objects.filter(is_active=True)
            .exclude(kind=GoodKind.RENTAL)
            .order_by("price", "name")[:goods]
        )
        court = Good.objects.filter(is_active=True, kind=GoodKind.RENTAL).order_by("price").first()
        if court is not None:
            picked.append(court)
        if not picked:
            raise CommandError("No goods for sale; add goods first (e.g. seed_till_goods).")
        preferential.save_settings(actor=None, enabled=True, levels=rules, goods=picked)

        current = PreferentialSettings.current()
        self.stdout.write(self.style.SUCCESS("Preferential service on."))
        for level in current.levels:
            self.stdout.write(
                f"  last month >= {level['threshold']} -> {level['items']} free items"
            )
        self.stdout.write("Preferential goods:")
        for good in picked:
            self.stdout.write(f"  {good.name} ({good.price}, {good.service_position.seller.name})")

        cards = dict(
            RFIDCardAssignment.objects.filter(unassigned_at__isnull=True).values_list(
                "developer_id", "card__uid"
            )
        )
        found = []
        for developer in Developer.objects.filter(status=DeveloperStatus.ACTIVE):
            usage = preferential.usage_last_month(developer.pk)
            items = preferential.items_for(current.levels, usage)
            if items and developer.pk in cards:
                found.append((usage, items, developer))
        found.sort(key=lambda row: row[0], reverse=True)
        self.stdout.write(f"Developers with free items this month: {len(found)}")
        for level in reversed(current.levels):
            rows = [row for row in found if row[1] == level["items"]][:3]
            if not rows:
                continue
            self.stdout.write(f"  {level['items']} items (spent >= {level['threshold']}):")
            for usage, _items, developer in rows:
                left = preferential.free_left(developer.pk)
                self.stdout.write(
                    f"    {developer.employee_number} {developer.full_name}: card "
                    f"{cards[developer.pk]}, spent {usage}, {left} left"
                )
