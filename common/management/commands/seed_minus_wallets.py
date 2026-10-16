import random
import re
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.developers.models import Developer, DeveloperStatus
from apps.finance import services as finance
from apps.finance.models import AccountStatus, AccountTransaction, TransactionKind, WalletSettings

KEY = "seed-minus-"  # each wallet's seeded entry: seed-minus-<developer id>
UNDO = "seed-minus-undo-"  # its reversal: seed-minus-undo-<developer id>
REASON = "Test seed: wallet in minus"


class Command(BaseCommand):
    help = (
        "Test data: takes a few developers' wallets below zero (one adjustment each, within "
        "the minus limit, which is raised if needed) so the minus figures can be tried. "
        "Running it again adds nothing for wallets already done; --undo puts every balance "
        "and the limit back. Test servers only (DEBUG or TEST_CONSOLE_ENABLED)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=5, help="Wallets to take below zero.")
        parser.add_argument(
            "--limit", default="20.00", help="Minus limit to use if the current one is lower."
        )
        parser.add_argument("--undo", action="store_true", help="Reverse what this seeded.")

    def handle(self, *args, count=5, limit="20.00", undo=False, **options):
        if not (settings.DEBUG or settings.TEST_CONSOLE_ENABLED):
            raise CommandError(
                "seed_minus_wallets only runs on a test server (DEBUG or TEST_CONSOLE_ENABLED)."
            )
        with transaction.atomic():
            if undo:
                self._undo()
            else:
                self._seed(count, finance.money(limit))
        problems = finance.ledger_mismatches()
        if problems:
            raise CommandError(f"Ledger check failed: {problems[:3]}")
        self.stdout.write(self.style.SUCCESS("Ledger reconciles."))

    @staticmethod
    def _seeded():
        return AccountTransaction.objects.filter(idempotency_key__startswith=KEY).exclude(
            idempotency_key__startswith=UNDO
        )

    @staticmethod
    def _undone() -> set[int]:
        keys = AccountTransaction.objects.filter(idempotency_key__startswith=UNDO)
        return {int(k.removeprefix(UNDO)) for k in keys.values_list("idempotency_key", flat=True)}

    def _seed(self, count: int, wanted: Decimal) -> None:
        if count < 1 or wanted <= 0:
            raise CommandError("--count and --limit must be more than 0.")
        before = finance.money(WalletSettings.current().minus_limit)
        if before < wanted:
            finance.set_minus_limit(actor=None, minus_limit=wanted)
            self.stdout.write(f"Minus limit {before} -> {wanted}")
        limit = WalletSettings.current().minus_limit

        done = set(self._seeded().values_list("account__developer_id", flat=True))
        still = done - self._undone()
        if still:
            self.stdout.write(f"{len(still)} wallet(s) already in minus from an earlier run.")
        candidates = (
            Developer.objects.filter(
                status=DeveloperStatus.ACTIVE,
                deleted_at__isnull=True,
                account__status=AccountStatus.ACTIVE,
            )
            .exclude(pk__in=done)
            .select_related("account", "building")
            .order_by("building__code", "employee_number")
        )
        # Spread over the buildings: one from each building in turn.
        by_building: dict = {}
        for dev in candidates:
            by_building.setdefault(dev.building_id, []).append(dev)
        picked = []
        while len(picked) < count and any(by_building.values()):
            for devs in by_building.values():
                if devs and len(picked) < count:
                    picked.append(devs.pop(0))
        if not picked:
            raise CommandError("No active developer wallet left to take below zero.")

        rng = random.Random(7)
        for dev in picked:
            owed = Decimal(rng.randint(100, int(limit * 100))) / 100  # 1.00 .. the limit
            txn = finance.post_transaction(
                account=dev.account,
                kind=TransactionKind.ADJUSTMENT,
                amount=-(dev.account.balance + owed),
                # The limit before seeding, so --undo can put it back.
                description=f"{REASON} (limit before: {before})",
                idempotency_key=f"{KEY}{dev.pk}",
                allow_minus=True,
            )
            building = dev.building.code if dev.building else "-"
            self.stdout.write(
                f"  {dev.employee_number:<12} {dev.full_name:<24} {building:<6} "
                f"balance {txn.balance_after}"
            )
        self.stdout.write(f"Took {len(picked)} wallet(s) below zero (minus limit {limit}).")

    def _undo(self) -> None:
        undone = self._undone()
        reversed_ = 0
        first_limit = None
        for txn in self._seeded().select_related("account").order_by("id"):
            if first_limit is None:
                match = re.search(r"limit before: ([\d.]+)", txn.description)
                first_limit = finance.money(match.group(1)) if match else None
            dev_id = txn.account.developer_id
            if dev_id in undone:
                continue
            finance.post_transaction(
                account=txn.account,
                kind=TransactionKind.ADJUSTMENT,
                amount=-txn.amount,
                description=f"{REASON} (undone)",
                idempotency_key=f"{UNDO}{dev_id}",
            )
            reversed_ += 1
        self.stdout.write(f"Reversed {reversed_} wallet(s).")
        if reversed_ and first_limit is not None:
            finance.set_minus_limit(actor=None, minus_limit=first_limit)
            self.stdout.write(f"Minus limit back to {first_limit}.")
