from django.core.management.base import BaseCommand

from apps.finance.services import ledger_mismatches


class Command(BaseCommand):
    help = "Verify every account balance equals its ledger. Exit code 1 on any mismatch."

    def handle(self, *args, **options):
        problems = ledger_mismatches()
        if not problems:
            self.stdout.write(self.style.SUCCESS("Ledger consistent."))
            return
        for p in problems:
            self.stdout.write(self.style.ERROR(f"Account {p['id']}: {p}"))
        raise SystemExit(1)
