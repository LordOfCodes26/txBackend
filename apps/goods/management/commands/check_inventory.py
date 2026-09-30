from django.core.management.base import BaseCommand

from apps.goods.services import stock_mismatches


class Command(BaseCommand):
    help = "Verify every good's quantity equals the sum of its inventory movements."

    def handle(self, *args, **options):
        mismatches = stock_mismatches()
        if not mismatches:
            self.stdout.write(self.style.SUCCESS("Inventory consistent."))
            return
        for g in mismatches:
            self.stdout.write(
                self.style.ERROR(
                    f"Good {g['id']} {g['name']!r}: quantity={g['quantity']} "
                    f"ledger={g['ledger'] or 0}"
                )
            )
        raise SystemExit(1)
