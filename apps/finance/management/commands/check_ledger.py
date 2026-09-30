from django.core.management.base import BaseCommand

from apps.finance.services import ledger_mismatches
from apps.seller_finance.services import seller_ledger_mismatches


class Command(BaseCommand):
    help = (
        "Verify developer and seller balances equal their ledgers, and seller sale credits "
        "equal confirmed purchases. Exit code 1 on any mismatch."
    )

    def handle(self, *args, **options):
        problems = [("Developer account", p) for p in ledger_mismatches()]
        problems += [("Seller account", p) for p in seller_ledger_mismatches()]
        if not problems:
            self.stdout.write(self.style.SUCCESS("Ledgers consistent."))
            return
        for label, p in problems:
            self.stdout.write(self.style.ERROR(f"{label} {p['id']}: {p}"))
        raise SystemExit(1)
