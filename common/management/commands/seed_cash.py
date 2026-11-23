import random
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.rbac import Roles
from apps.audit.models import AuditLog
from apps.cash import services as cash
from apps.cash.models import (
    CashEntry,
    CashKind,
    CashRequest,
    CountOutcome,
    RequestKind,
    RequestStatus,
    StockCount,
    StockCountLine,
)
from apps.finance.services import money
from apps.goods import services as goods
from apps.goods.models import Good, GoodKind, InventoryMovement, MovementKind
from apps.purchases.models import Payment, Purchase, PurchaseItem, PurchaseStatus
from apps.sellers import services as sellers
from apps.sellers.models import Seller, SellerStatus
from common.record_delete import _writable

KEY = "seed-cash-"  # purchases and cash entries: their idempotency keys start with it
DELIVERY = "seed-cash-delivery"  # its stock movements' reference
MARK = "(demo)"  # requests and stock counts: their reason or note ends with it
ZERO = Decimal("0.00")
BUYERS = ["", "", "", "Visitor Kim", "Ri (driver)", "Guest, floor 2", "Pak (cleaning)", "Delivery"]


class Command(BaseCommand):
    help = (
        "Test data: cash sales of the last days at the demo stores, with handovers to finance, "
        "deleted sales, requests waiting for approval and stock counts, for Cash control. "
        "Switches cash on for those stores. Stock and prices stay as they are now. Stores that "
        "already have cash entries are left alone; --undo removes what this seeded. Test "
        "servers only (DEBUG or TEST_CONSOLE_ENABLED)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=14, help="Days of sales, up to now.")
        parser.add_argument(
            "--store", action="append", help="A store's name (again for more). Default: Demo ..."
        )
        parser.add_argument("--undo", action="store_true", help="Remove what this seeded.")

    def handle(self, *args, days=14, store=None, undo=False, **options):
        if not (settings.DEBUG or settings.TEST_CONSOLE_ENABLED):
            raise CommandError(
                "seed_cash only runs on a test server (DEBUG or TEST_CONSOLE_ENABLED)."
            )
        with transaction.atomic():
            if undo:
                self._undo()
            else:
                self._seed(days, store)
            # Inside the transaction: if the books don't add up, nothing is kept.
            problems = cash.ledger_mismatches() + list(goods.stock_mismatches())
            if problems:
                raise CommandError(f"Reconciliation failed: {problems[:3]}")
        self.stdout.write(self.style.SUCCESS("Cash ledger and stock reconcile."))

    # -- seeding ---------------------------------------------------------------------------

    def _seed(self, days, names):
        if not 7 <= days <= 90:
            raise CommandError("--days must be 7 to 90.")
        admin = self._staff(Roles.ADMIN)
        self.finance = self._staff(Roles.FINANCE_MANAGER) or admin
        if self.finance is None:
            raise CommandError("No active finance manager or admin.")
        wanted = Seller.objects.filter(status=SellerStatus.ACTIVE).order_by("pk")
        wanted = wanted.filter(name__in=names) if names else wanted.filter(name__startswith="Demo ")
        if names and wanted.count() != len(set(names)):
            raise CommandError("No active store with one of these names.")
        done = 0
        for seller in wanted:
            if CashEntry.objects.filter(seller=seller).exists():
                self.stdout.write(f"  {seller.name}: already has cash entries, left alone.")
                continue
            catalog = list(
                Good.objects.filter(service_position__seller=seller, is_active=True, price__gt=0)
                .filter(service_position__is_active=True)
                .exclude(kind=GoodKind.RENTAL)
                .order_by("pk")
            )
            if not catalog:
                self.stdout.write(f"  {seller.name}: no goods, left alone.")
                continue
            self._store(seller, catalog, days, seller.user or admin)
            done += 1
        if not done:
            raise CommandError("No store left to seed.")

    @staticmethod
    def _staff(role):
        return (
            User.objects.filter(is_active=True, user_roles__role__code=role).order_by("pk").first()
        )

    def _store(self, seller, catalog, days, clerk):
        rng = random.Random(seller.pk * 1000 + days)
        now = timezone.now()
        today = timezone.localdate()
        first = today - timedelta(days=days - 1)
        switched = not seller.accepts_cash
        if switched:
            sellers.update_seller(actor=None, seller=seller, accepts_cash=True)

        # A few goods of one counter in each sale; dear goods one at a time, and rarely.
        counters: dict[int, list[Good]] = {}
        for good in catalog:
            counters.setdefault(good.service_position_id, []).append(good)
        dear = min(g.price for g in catalog) > 20
        sales = []
        for n in range(days):
            day = first + timedelta(days=n)
            for _i in range(rng.randint(0, 1) if dear else rng.randint(2, 6)):
                moment = _at(day, time(rng.randint(8, 19), rng.randint(0, 59), rng.randint(0, 59)))
                if moment > now - timedelta(minutes=5):
                    continue
                own = rng.choice(list(counters.values()))
                lines = {}
                for good in rng.sample(own, k=min(len(own), rng.randint(1, 3))):
                    if good.decimal_quantity:
                        lines[good] = Decimal(rng.choice(["0.50", "0.75", "1.20", "2.00"]))
                    else:
                        lines[good] = Decimal(1 if good.price > 20 else rng.choice([1, 1, 2, 3]))
                sales.append({"moment": moment, "lines": lines, "buyer": rng.choice(BUYERS)})
        sales.sort(key=lambda s: s["moment"])
        if not sales:
            raise CommandError(f"{seller.name}: no sales fell in these days; use more --days.")

        # What happens to some of the sales: deleted (a day later), refused, still waiting.
        old = [s for s in sales if s["moment"] < now - timedelta(days=2)]
        rng.shuffle(old)
        spare = {g.pk: g.quantity for g in catalog if g.track_stock}
        fates = [
            ("APPROVED", "Buyer brought it back"),
            ("APPROVED", "Rang it up twice"),
            ("REJECTED", "Wrong goods"),
        ]
        for sale in old:
            if not fates:
                break
            # A deleted sale was out of stock for a day: only where there is enough now.
            taken = {g.pk: q for g, q in sale["lines"].items() if g.track_stock}
            if any(spare[pk] < quantity for pk, quantity in taken.items()):
                continue
            for pk, quantity in taken.items():
                spare[pk] -= quantity
            sale["fate"] = fates.pop(0)
        sales[-1].setdefault("fate", ("PENDING", "Wrong quantity"))

        # Three stock counts: explained, charged to the store, and one still to explain.
        tracked = [g for g in catalog if g.track_stock]
        counts = []
        for back, outcome, note in (
            (max(days - 3, 5), CountOutcome.EXPLAINED, "Broken bottles, shown to finance"),
            (4, CountOutcome.CHARGED, "No explanation"),
            (1, CountOutcome.OPEN, ""),
        ):
            if not tracked:
                break
            picked = rng.sample(tracked, k=min(len(tracked), 5))
            lines = {g: 0 for g in picked}
            for good in picked[: rng.randint(1, 2)]:
                lines[good] = 1 if good.price > 20 else rng.randint(1, 3)
            counts.append(
                {
                    "moment": _at(today - timedelta(days=back), time(7, 30)),
                    "lines": lines,
                    "outcome": outcome,
                    "note": note,
                }
            )

        self._write(seller, clerk, first, sales, counts, rng)
        self._requests(seller, clerk, catalog, rng, now)
        self.stdout.write(
            f"  {seller.name}: {len(sales)} cash sales, owes {cash.owed(seller.pk)}"
            f"{', cash switched on' if switched else ''}."
        )

    def _write(self, seller, clerk, first, sales, counts, rng):
        tag = f"{KEY}{seller.pk}-"
        self.owed = ZERO
        self.serial = 0
        # A delivery covering what goes out for good, so today's stock stays as it is.
        need: dict[Good, Decimal] = {}
        for sale in sales:
            back = sale.get("fate", ("",))[0] == "APPROVED"  # sold, and back a day later
            for good, quantity in sale["lines"].items():
                if good.track_stock:
                    need[good] = need.get(good, ZERO) + (ZERO if back else quantity)
        for count in counts:
            for good, missing in count["lines"].items():
                need[good] = need.get(good, ZERO) + missing
        start = _at(first, time(6))
        stock = {}
        for good, quantity in need.items():
            stock[good.pk] = good.quantity
            self._move(stock, good, MovementKind.RESTOCK, quantity, start, DELIVERY, "Delivery")

        events = [(s["moment"], "sale", s) for s in sales]
        events += [
            (s["moment"] + timedelta(hours=20), "return", s)
            for s in sales
            if s.get("fate", ("",))[0] == "APPROVED"
        ]
        events += [(c["moment"], "count", c) for c in counts]
        # Finance comes in the evening, not on Sundays and not today.
        wrong = None
        day = first
        while day < timezone.localdate():
            if day.weekday() != 6:
                events.append((_at(day, time(20, 30)), "handover", None))
                wrong = wrong or day
            day += timedelta(days=1)
        for moment, kind, what in sorted(events, key=lambda e: (e[0], e[1])):
            if kind == "sale":
                total = ZERO
                purchase = Purchase.objects.create(
                    seller=seller,
                    service_position=next(iter(what["lines"])).service_position,
                    created_by=clerk,
                )
                reference = f"purchase:{purchase.pk}"
                for good, quantity in what["lines"].items():
                    line = money(good.price * quantity)
                    total += line
                    PurchaseItem.objects.create(
                        purchase=purchase,
                        good=good,
                        quantity=quantity,
                        unit_price=good.price,
                        line_total=line,
                    )
                    if good.track_stock:
                        self._move(stock, good, MovementKind.SALE, -quantity, moment, reference)
                what["purchase"], what["total"] = purchase, total
                Purchase.objects.filter(pk=purchase.pk).update(
                    status=PurchaseStatus.CONFIRMED,
                    payment=Payment.CASH,
                    buyer_name=what["buyer"],
                    total=total,
                    confirm_idempotency_key=f"{tag}sale-{purchase.pk}",
                    confirmed_by=clerk,
                    confirmed_at=moment,
                    created_at=moment - timedelta(minutes=2),
                    updated_at=moment,
                )
                self._entry(seller, CashKind.SALE, total, moment, clerk, reference=reference)
                self._wish(seller, clerk, what, moment)
            elif kind == "return":
                purchase, total = what["purchase"], what["total"]
                reference = f"purchase:{purchase.pk}"
                for good, quantity in what["lines"].items():
                    if good.track_stock:
                        self._move(stock, good, MovementKind.RETURN, quantity, moment, reference)
                self._entry(
                    seller,
                    CashKind.RETURN,
                    -total,
                    moment,
                    self.finance,
                    note=what["fate"][1],
                    reference=reference,
                )
                Purchase.objects.filter(pk=purchase.pk).update(
                    status=PurchaseStatus.REFUNDED,
                    refunded_by=self.finance,
                    refunded_at=moment,
                    refund_reason=what["fate"][1],
                    updated_at=moment,
                )
            elif kind == "handover" and self.owed > 0:
                if wrong is not None and moment.date() > wrong + timedelta(days=2):
                    # Once: typed wrong, cancelled, entered again.
                    typo = self._entry(
                        seller, CashKind.HANDOVER, -(self.owed * 10), moment, self.finance
                    )
                    moment += timedelta(minutes=3)
                    self._entry(
                        seller,
                        CashKind.HANDOVER,
                        -typo.amount,
                        moment,
                        self.finance,
                        note="Typed wrong",
                        reference=f"reverses:{typo.pk}",
                        reverses=typo,
                    )
                    moment += timedelta(minutes=1)
                    wrong = None
                short = rng.random() < 0.25  # some cash stays in the drawer
                amount = money(self.owed * Decimal("0.80")) if short else self.owed
                self._entry(
                    seller,
                    CashKind.HANDOVER,
                    -amount,
                    moment,
                    self.finance,
                    note="Change left in the drawer" if short else "",
                )
            elif kind == "count":
                self._count(seller, what, stock, moment)

    def _move(self, stock, good, kind, delta, moment, reference, reason=""):
        if not delta:
            return
        stock[good.pk] += delta
        InventoryMovement.objects.create(
            good=good,
            kind=kind,
            quantity_delta=delta,
            quantity_after=stock[good.pk],
            reason=reason,
            reference=reference,
            created_at=moment,
        )

    def _entry(self, seller, kind, amount, moment, actor, note="", reference="", reverses=None):
        self.owed += amount
        self.serial += 1
        return CashEntry.objects.create(
            seller=seller,
            kind=kind,
            amount=amount,
            owed_after=self.owed,
            note=note,
            reference=reference,
            idempotency_key=f"{KEY}{seller.pk}-{self.serial}",
            reverses=reverses,
            actor=actor,
            created_at=moment,
        )

    def _wish(self, seller, clerk, sale, moment):
        if "fate" not in sale:
            return
        status, reason = sale["fate"]
        wish = CashRequest.objects.create(
            kind=RequestKind.DELETE_SALE,
            seller=seller,
            purchase=sale["purchase"],
            reason=f"{reason} {MARK}",
            requested_by=clerk,
            status=status,
            decided_by=None if status == "PENDING" else self.finance,
            decided_at=None if status == "PENDING" else moment + timedelta(hours=20),
            decision_note="The goods did not come back" if status == "REJECTED" else "",
        )
        asked = moment + timedelta(minutes=4)
        CashRequest.objects.filter(pk=wish.pk).update(created_at=asked, updated_at=asked)

    def _count(self, seller, plan, stock, moment):
        count = StockCount.objects.create(
            seller=seller, counted_by=self.finance, note=f"Surprise count {MARK}"
        )
        missing = ZERO
        for good, lost in plan["lines"].items():
            expected = stock[good.pk]
            StockCountLine.objects.create(
                count=count,
                good=good,
                expected=expected,
                counted=expected - lost,
                price=good.price,
            )
            missing += money(good.price * lost)
            self._move(
                stock,
                good,
                MovementKind.ADJUSTMENT,
                -Decimal(lost),
                moment,
                f"stock_count:{count.pk}",
                f"Stock count {count.pk}",
            )
        settled = plan["outcome"] != CountOutcome.OPEN
        charge = None
        if plan["outcome"] == CountOutcome.CHARGED:
            charge = self._entry(
                seller,
                CashKind.MISSING,
                missing,
                moment + timedelta(hours=1),
                self.finance,
                note=plan["note"],
                reference=f"stock_count:{count.pk}",
            )
        count.missing_value = missing
        count.outcome = plan["outcome"]
        count.outcome_note = plan["note"]
        count.charge = charge
        count.resolved_by = self.finance if settled else None
        count.resolved_at = moment + timedelta(hours=1) if settled else None
        count.save()
        StockCount.objects.filter(pk=count.pk).update(created_at=moment, updated_at=moment)

    def _requests(self, seller, clerk, catalog, rng, now):
        """A price refused, a price waiting, and a new good waiting."""
        free = [g for g in catalog if not g.cash_requests.filter(status="PENDING").exists()]
        picked = rng.sample(free, k=min(len(free), 2))
        plans = [(RequestStatus.PENDING, Decimal("0.80"), timedelta(hours=3), "Slow seller")]
        plans.append((RequestStatus.REJECTED, Decimal("0.50"), timedelta(days=3), "Too many left"))
        for good, (status, factor, ago, reason) in zip(picked, plans, strict=False):
            wish = CashRequest.objects.create(
                kind=RequestKind.PRICE,
                seller=seller,
                good=good,
                old_price=good.price,
                new_price=money(good.price * factor),
                reason=f"{reason} {MARK}",
                requested_by=clerk,
                status=status,
                decided_by=self.finance if status == RequestStatus.REJECTED else None,
                decided_at=now - ago + timedelta(hours=2)
                if status == RequestStatus.REJECTED
                else None,
                decision_note="Too low" if status == RequestStatus.REJECTED else "",
            )
            CashRequest.objects.filter(pk=wish.pk).update(
                created_at=now - ago, updated_at=now - ago
            )
        new = Good.objects.create(
            service_position=catalog[0].service_position,
            name=f"Visitor special {MARK}",
            price="5.00",
            track_stock=False,
            is_active=False,
        )
        CashRequest.objects.create(
            kind=RequestKind.NEW_GOOD,
            seller=seller,
            good=new,
            new_price=new.price,
            reason=f"For visitors {MARK}",
            requested_by=clerk,
        )

    # -- undoing ---------------------------------------------------------------------------

    def _undo(self):
        bought = Purchase.objects.filter(confirm_idempotency_key__startswith=KEY)
        counted = StockCount.objects.filter(note__endswith=MARK)
        wishes = CashRequest.objects.filter(reason__endswith=MARK)
        purchases = list(bought.values_list("pk", flat=True))
        counts = list(counted.values_list("pk", flat=True))
        seeded = CashEntry.objects.filter(idempotency_key__startswith=KEY)
        stores = set(seeded.values_list("seller", flat=True)) | set(
            bought.values_list("seller", flat=True)
        )
        if not stores:
            raise CommandError("Nothing seeded to undo.")
        references = [f"purchase:{pk}" for pk in purchases] + [f"stock_count:{pk}" for pk in counts]
        # Also what people did with the seeded rows since (a request approved, a count charged,
        # a seeded handover cancelled): it goes with them.
        base = set(
            CashEntry.objects.filter(
                Q(idempotency_key__startswith=KEY) | Q(reference__in=references)
            ).values_list("pk", flat=True)
        )
        entries = CashEntry.objects.filter(Q(pk__in=base) | Q(reverses__in=base))
        moves = InventoryMovement.objects.filter(
            Q(reference__in=references) | Q(reference=DELIVERY)
        )
        deltas = dict(
            moves.order_by()
            .values("good")
            .annotate(t=Sum("quantity_delta"))
            .values_list("good", "t")
        )
        for good_id, total in deltas.items():
            good = Good.all_objects.select_for_update().get(pk=good_id)
            if good.quantity - total < 0:
                raise CommandError(f"{good.name}: not enough stock left to undo.")
            Good.all_objects.filter(pk=good_id).update(quantity=good.quantity - total)
        # A seeded price wish approved since: the old price again.
        for wish in wishes.filter(kind=RequestKind.PRICE, status=RequestStatus.APPROVED):
            if wish.good.price == wish.new_price:
                goods.update_good(actor=None, good=wish.good, price=wish.old_price)
        new = list(wishes.filter(kind=RequestKind.NEW_GOOD).values_list("good", flat=True))
        with _writable(CashEntry, InventoryMovement):
            moves.delete()
            counted.delete()
            wishes.delete()
            gone = list(entries.values_list("pk", flat=True))
            CashEntry.objects.filter(pk__in=gone, reverses__isnull=False).delete()
            CashEntry.objects.filter(pk__in=gone).delete()
            Purchase.objects.filter(pk__in=purchases).delete()
            # Real entries made meanwhile stay, with what was owed after each worked out anew.
            for seller_id in stores:
                owed = ZERO
                for entry in CashEntry.objects.filter(seller=seller_id).order_by("id"):
                    owed += entry.amount
                    CashEntry.objects.filter(pk=entry.pk).update(owed_after=owed)
        kept = 0
        for good in Good.all_objects.filter(pk__in=new):
            if good.purchase_items.exists() or good.movements.exists():
                kept += 1
            else:
                good.delete()
        for seller in Seller.objects.filter(pk__in=stores, accepts_cash=True):
            # Off again where this switched it on (no one's name on it) and it is not in use.
            last = (
                AuditLog.objects.filter(
                    action="seller.updated",
                    entity_id=str(seller.pk),
                    new_values__has_key="accepts_cash",
                )
                .order_by("-id")
                .first()
            )
            mine = last is not None and not last.actor_username
            if mine and not CashEntry.objects.filter(seller=seller).exists():
                sellers.update_seller(actor=None, seller=seller, accepts_cash=False)
                self.stdout.write(f"  {seller.name}: cash switched off again.")
        self.stdout.write(
            f"Removed {len(purchases)} cash sales, {len(gone)} cash entries and "
            f"{len(counts)} stock counts."
            + (f" Kept {kept} new good(s) sold since." if kept else "")
        )


def _at(day, at) -> datetime:
    return timezone.make_aware(datetime.combine(day, at))
