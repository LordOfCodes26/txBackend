import random
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.attendance.models import DailyAttendance
from apps.audit.models import AuditLog
from apps.finance.models import AccountTransaction, DeveloperAccount, TransactionKind
from apps.finance.services import ledger_mismatches
from apps.goods.models import GoodKind, InventoryMovement, MovementKind
from apps.goods.services import stock_mismatches
from apps.purchases.models import Purchase, PurchaseItem, PurchaseStatus
from apps.rfid.models import (
    DevicePurpose,
    RFIDCardAssignment,
    RFIDDevice,
    RFIDEvent,
    ScanResult,
)
from apps.seller_finance.models import (
    PayoutStatus,
    SellerAccount,
    SellerPayment,
    SellerTransaction,
    SellerTransactionKind,
)
from apps.seller_finance.services import seller_ledger_mismatches
from apps.sellers.models import ServicePosition

MARK = "demo-month"
ZERO = Decimal("0.00")


class Command(BaseCommand):
    help = (
        "Demo data: a month of purchases by developers who were present (per attendance), "
        "with ledger, seller and stock entries dated in that month. Each month is closed "
        "out (allowance deposit, seller payout, stock delivery) so today's balances and stock "
        "stay exactly as they are. Runs once per month."
    )

    def add_arguments(self, parser):
        parser.add_argument("--month", help="YYYY-MM (default: last month)")

    def handle(self, *args, month=None, **options):
        if not settings.DEBUG:
            raise CommandError("seed_purchases_month only runs with DEBUG=True.")
        first, last = _month_bounds(month)
        tag = f"{MARK}-{first:%Y%m}"
        if Purchase.objects.filter(confirm_idempotency_key__startswith=tag).exists():
            raise CommandError(f"Purchases for {first:%Y-%m} were already seeded.")

        self.rng = random.Random(first.toordinal())
        counters = self._counters()
        presence = list(
            DailyAttendance.objects.filter(work_date__range=(first, last))
            .select_related("developer")
            .order_by("work_date", "developer_id")
        )
        if not presence:
            raise CommandError("No attendance in that month; run seed_attendance_month first.")
        cards = {
            a.developer_id: a.card
            for a in RFIDCardAssignment.objects.filter(unassigned_at__isnull=True).select_related(
                "card"
            )
        }

        plans = self._plan(presence, cards, counters)
        with transaction.atomic():
            self._write(plans, first, last, tag)
        self.stdout.write(f"Created {len(plans)} purchases for {first:%Y-%m}.")

        problems = ledger_mismatches() + seller_ledger_mismatches() + stock_mismatches()
        if problems:
            raise CommandError(f"Reconciliation failed: {problems[:3]}")
        self.stdout.write(self.style.SUCCESS("Ledgers, seller sales and stock reconcile."))

    # -- planning --------------------------------------------------------------------------

    def _counters(self):
        readers = list(
            RFIDDevice.objects.filter(purpose=DevicePurpose.TILL, is_active=True).order_by("code")
        )
        counters = []
        for i, position in enumerate(
            ServicePosition.objects.filter(is_active=True, seller__status="ACTIVE")
            .select_related("seller")
            .order_by("pk")
        ):
            goods = list(
                position.goods.filter(is_active=True).exclude(kind=GoodKind.RENTAL).order_by("pk")
            )
            if goods and readers:
                weight = 1 if "Tech" in position.seller.name else 10
                counters.append((position, goods, readers[i % len(readers)], weight))
        if not counters:
            raise CommandError("No counters with goods; run seed_demo / seed_more first.")
        return counters

    def _plan(self, presence, cards, counters):
        plans = []
        for day in presence:
            card = cards.get(day.developer_id)
            saturday = day.work_date.weekday() == 5
            if card is None or day.last_seen is None:
                continue
            chance = 0.15 if saturday else 0.35
            n = 0
            while self.rng.random() < chance and n < 2:
                n += 1
                chance = 0.25  # a second purchase is less likely
                position, goods, reader, _ = self.rng.choices(
                    counters, weights=[c[3] for c in counters]
                )[0]
                lines = {}
                for good in self.rng.sample(goods, k=min(len(goods), self.rng.randint(1, 3))):
                    qty = 1 if good.price > 20 else self.rng.choice([1, 1, 1, 2])
                    lines[good] = qty
                span = (day.last_seen - day.first_seen).total_seconds()
                moment = day.first_seen + timedelta(seconds=self.rng.uniform(0.1, 0.9) * span)
                plans.append((moment, day.developer, card, position, reader, lines))
        plans.sort(key=lambda p: p[0])
        return plans

    # -- writing ----------------------------------------------------------------------------

    def _write(self, plans, first, last, tag):
        accounts = {a.developer_id: a for a in DeveloperAccount.objects.all()}
        spend = defaultdict(lambda: ZERO)
        sales = defaultdict(lambda: ZERO)
        sold = defaultdict(int)
        for _, dev, _, position, _, lines in plans:
            total = sum((g.price * q for g, q in lines.items()), ZERO)
            spend[dev.pk] += total
            sales[position.seller_id] += total
            for g, q in lines.items():
                if g.track_stock:
                    sold[g.pk] += q

        month_start = _at(first, 6)
        month_end = _at(last, 23)

        # Monthly allowance covering the month's spending (keeps today's balances intact).
        running = {}
        for dev_id, amount in spend.items():
            account = accounts[dev_id]
            AccountTransaction.objects.create(
                account=account,
                kind=TransactionKind.DEPOSIT,
                amount=amount,
                balance_after=amount,
                description=f"Monthly allowance ({first:%b %Y})",
                idempotency_key=f"{tag}-dep-{account.pk}",
                created_at=month_start,
            )
            running[dev_id] = amount
        # Deliveries covering what sells (keeps today's stock intact).
        stock = {}
        for good_id, qty in sold.items():
            InventoryMovement.objects.create(
                good_id=good_id,
                kind=MovementKind.RESTOCK,
                quantity_delta=qty,
                quantity_after=qty,
                reason=f"Delivery ({first:%b %Y})",
                created_at=month_start - timedelta(hours=1),
            )
            stock[good_id] = qty
        seller_running = defaultdict(lambda: ZERO)
        seller_accounts = {a.seller_id: a for a in SellerAccount.objects.all()}

        for i, (moment, dev, card, position, reader, lines) in enumerate(plans):
            account = accounts[dev.pk]
            total = sum((g.price * q for g, q in lines.items()), ZERO)
            tap = RFIDEvent.objects.create(
                device=reader,
                client_event_id=f"{tag}-tap-{i}",
                uid=card.uid,
                card=card,
                developer=dev,
                event_time=moment - timedelta(seconds=20),
                received_at=moment - timedelta(seconds=20),
                result=ScanResult.ACCEPTED,
            )
            purchase = Purchase.objects.create(
                seller=position.seller,
                service_position=position,
                reader=reader,
                created_by=position.seller.user,
            )
            reference = f"purchase:{purchase.pk}"
            for good, qty in lines.items():
                PurchaseItem.objects.create(
                    purchase=purchase,
                    good=good,
                    quantity=qty,
                    unit_price=good.price,
                    line_total=good.price * qty,
                )
                if good.track_stock:
                    stock[good.pk] -= qty
                    InventoryMovement.objects.create(
                        good=good,
                        kind=MovementKind.SALE,
                        quantity_delta=-qty,
                        quantity_after=stock[good.pk],
                        reference=reference,
                        created_at=moment,
                    )
            running[dev.pk] -= total
            txn = AccountTransaction.objects.create(
                account=account,
                kind=TransactionKind.PURCHASE,
                amount=-total,
                balance_after=running[dev.pk],
                description=f"Purchase at {position.seller.name}",
                reference=reference,
                created_at=moment,
            )
            seller_running[position.seller_id] += total
            SellerTransaction.objects.create(
                account=seller_accounts[position.seller_id],
                kind=SellerTransactionKind.SALE,
                amount=total,
                balance_after=seller_running[position.seller_id],
                description="Sale",
                reference=reference,
                created_at=moment,
            )
            Purchase.objects.filter(pk=purchase.pk).update(
                status=PurchaseStatus.CONFIRMED,
                developer=dev,
                card=card,
                total=total,
                account_transaction=txn,
                presented_event=tap,
                presented_at=tap.received_at,
                confirm_idempotency_key=f"{tag}-{i}",
                confirmed_by=position.seller.user,
                confirmed_at=moment,
                created_at=moment - timedelta(minutes=2),
                updated_at=moment,
            )
            AuditLog.objects.create(
                actor=position.seller.user,
                actor_username=getattr(position.seller.user, "username", ""),
                action="purchase.confirmed",
                entity_type="purchases.purchase",
                entity_id=str(purchase.pk),
                new_values={"developer": dev.pk, "total": str(total)},
                created_at=moment,
            )

        # Month-end payout of each seller's sales (keeps today's seller balances intact).
        for seller_id, amount in sales.items():
            payment = SellerPayment.objects.create(
                seller_id=seller_id,
                amount=amount,
                status=PayoutStatus.REQUESTED,
                note=f"Payout {first:%b %Y}",
                idempotency_key=f"{tag}-payout-{seller_id}",
            )
            txn = SellerTransaction.objects.create(
                account=seller_accounts[seller_id],
                kind=SellerTransactionKind.PAYOUT,
                amount=-amount,
                balance_after=ZERO,
                description=f"Payout ({first:%b %Y})",
                reference=f"payout:{payment.pk}",
                created_at=month_end,
            )
            SellerPayment.objects.filter(pk=payment.pk).update(
                status=PayoutStatus.PAID,
                transaction=txn,
                payment_reference=f"DEMO-{first:%Y%m}",
                approved_at=month_end - timedelta(hours=2),
                paid_at=month_end,
                created_at=month_end - timedelta(days=1),
                updated_at=month_end,
            )


def _at(day: date, hour: int) -> datetime:
    return timezone.make_aware(datetime.combine(day, time(hour)))


def _month_bounds(month: str | None) -> tuple[date, date]:
    if month:
        first = datetime.strptime(month, "%Y-%m").date()
    else:
        first = (timezone.localdate().replace(day=1) - timedelta(days=1)).replace(day=1)
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, nxt - timedelta(days=1)
