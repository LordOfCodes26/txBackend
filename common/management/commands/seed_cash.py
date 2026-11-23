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
from apps.finance.services import money
from apps.goods import services as goods
from apps.goods.models import Good, GoodKind, InventoryMovement, MovementKind
from apps.purchases.models import Payment, Purchase, PurchaseItem, PurchaseStatus
from apps.sellers import services as sellers
from apps.sellers.models import Seller, SellerStatus
from common.record_delete import _writable

KEY = "seed-cash-"  # purchases: their idempotency keys start with it
DELIVERY = "seed-cash-delivery"  # its stock movements' reference
MARK = "(demo)"  # new goods: their name ends with it
LOGGED = "seed-cash"  # its audit entries' request id
ZERO = Decimal("0.00")
BUYERS = ["", "", "", "Visitor Kim", "Ri (driver)", "Guest, floor 2", "Pak (cleaning)", "Delivery"]


class Command(BaseCommand):
    help = (
        "Test data: cash sales of the last days at the demo stores, with deleted sales and price "
        "changes. "
        "Switches cash on for those stores. Stock and prices stay as they are now. Stores that "
        "already have cash sales are left alone; --undo removes what this seeded. Test "
        "servers only (DEBUG or TEST_CONSOLE_ENABLED)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=14, help="Days of sales, up to now.")
        parser.add_argument(
            "--store", action="append", help="A store's name (again for more). Default: Demo ..."
        )
        parser.add_argument("--per-day", type=int, default=2, help="Sales a day and store, about.")
        parser.add_argument("--undo", action="store_true", help="Remove what this seeded.")

    def handle(self, *args, days=14, store=None, per_day=2, undo=False, **options):
        if not (settings.DEBUG or settings.TEST_CONSOLE_ENABLED):
            raise CommandError(
                "seed_cash only runs on a test server (DEBUG or TEST_CONSOLE_ENABLED)."
            )
        with transaction.atomic():
            if undo:
                self._undo()
            else:
                self._seed(days, store, per_day)
            # Inside the transaction: if the books don't add up, nothing is kept.
            problems = list(goods.stock_mismatches())
            if problems:
                raise CommandError(f"Reconciliation failed: {problems[:3]}")
        self.stdout.write(self.style.SUCCESS("Stock reconciles."))

    # -- seeding ---------------------------------------------------------------------------

    def _seed(self, days, names, per_day):
        if not 7 <= days <= 90:
            raise CommandError("--days must be 7 to 90.")
        if not 1 <= per_day <= 200:
            raise CommandError("--per-day must be 1 to 200.")
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
            if Purchase.objects.filter(seller=seller, payment=Payment.CASH).exists():
                self.stdout.write(f"  {seller.name}: already has cash sales, left alone.")
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
            self._store(seller, catalog, days, per_day, seller.user or admin)
            done += 1
        if not done:
            raise CommandError("No store left to seed.")

    @staticmethod
    def _staff(role):
        return (
            User.objects.filter(is_active=True, user_roles__role__code=role).order_by("pk").first()
        )

    def _store(self, seller, catalog, days, per_day, clerk):
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
            about = max(per_day // 5, 1) if dear else per_day
            for _i in range(rng.randint(about * 6 // 10, about * 14 // 10)):
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

        # Some sales are deleted by finance a day later.
        old = [s for s in sales if s["moment"] < now - timedelta(days=2)]
        rng.shuffle(old)
        spare = {g.pk: g.quantity for g in catalog if g.track_stock}
        reasons = ["Buyer brought it back", "Rang it up twice", "Wrong goods"]
        wanted = max(2, len(sales) // 80)
        for sale in old:
            if not wanted:
                break
            # A deleted sale was out of stock for a day: only where there is enough now.
            taken = {g.pk: q for g, q in sale["lines"].items() if g.track_stock}
            if any(spare[pk] < quantity for pk, quantity in taken.items()):
                continue
            for pk, quantity in taken.items():
                spare[pk] -= quantity
            sale["deleted"] = reasons[wanted % len(reasons)]
            wanted -= 1

        self._write(seller, clerk, first, sales, rng)
        self._prices(clerk, catalog, rng, now)
        self.stdout.write(
            f"  {seller.name}: {len(sales)} cash sales{', cash switched on' if switched else ''}."
        )

    def _write(self, seller, clerk, first, sales, rng):
        tag = f"{KEY}{seller.pk}-"
        # A delivery covering what goes out for good, so today's stock stays as it is.
        need: dict[Good, Decimal] = {}
        for sale in sales:
            back = "deleted" in sale  # sold, and back a day later
            for good, quantity in sale["lines"].items():
                if good.track_stock:
                    need[good] = need.get(good, ZERO) + (ZERO if back else quantity)
        start = _at(first, time(6))
        stock = {}
        for good, quantity in need.items():
            stock[good.pk] = good.quantity
            self._move(stock, good, MovementKind.RESTOCK, quantity, start, DELIVERY, "Delivery")

        events = [(s["moment"], "sale", s) for s in sales]
        events += [
            (s["moment"] + timedelta(hours=20), "return", s) for s in sales if "deleted" in s
        ]
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
            elif kind == "return":
                purchase, total = what["purchase"], what["total"]
                reference = f"purchase:{purchase.pk}"
                for good, quantity in what["lines"].items():
                    if good.track_stock:
                        self._move(stock, good, MovementKind.RETURN, quantity, moment, reference)
                Purchase.objects.filter(pk=purchase.pk).update(
                    status=PurchaseStatus.REFUNDED,
                    refunded_by=self.finance,
                    refunded_at=moment,
                    refund_reason=what["deleted"],
                    updated_at=moment,
                )

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

    def _prices(self, clerk, catalog, rng, now):
        """Audit entries as the goods page makes them: a price lowered and raised again (the
        prices stay as they are), one that went up to what it is now, and a new good."""

        def log(good, action, old, new, ago):
            AuditLog.objects.create(
                actor=clerk,
                actor_username=getattr(clerk, "username", ""),
                action=action,
                entity_type=Good._meta.label_lower,
                entity_id=str(good.pk),
                old_values=old,
                new_values=new,
                request_id=LOGGED,
                created_at=now - ago,
            )

        picked = rng.sample(catalog, k=min(len(catalog), 2))
        good = picked[0]
        now_price, low = str(good.price), str(money(good.price * Decimal("0.60")))
        start = timedelta(days=3, hours=2)
        log(good, "good.updated", {"price": now_price}, {"price": low}, start)
        back = start - timedelta(minutes=35)
        log(good, "good.updated", {"price": low}, {"price": now_price}, back)
        if len(picked) > 1:
            good = picked[1]
            before = str(money(good.price * Decimal("0.90")))
            log(
                good,
                "good.updated",
                {"price": before},
                {"price": str(good.price)},
                timedelta(days=6, hours=5),
            )
        new = Good.objects.create(
            service_position=catalog[0].service_position,
            name=f"Visitor special {MARK}",
            price="5.00",
            track_stock=False,
        )
        log(new, "good.created", None, {"price": "5.00", "name": new.name}, timedelta(hours=20))

    # -- undoing ---------------------------------------------------------------------------

    def _undo(self):
        bought = Purchase.objects.filter(confirm_idempotency_key__startswith=KEY)
        purchases = list(bought.values_list("pk", flat=True))
        stores = set(bought.values_list("seller", flat=True))
        if not stores:
            raise CommandError("Nothing seeded to undo.")
        references = [f"purchase:{pk}" for pk in purchases]
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
        with _writable(InventoryMovement, AuditLog):
            logged = AuditLog.objects.filter(request_id=LOGGED)
            added = list(logged.filter(action="good.created").values_list("entity_id", flat=True))
            logged.delete()
            moves.delete()
            Purchase.objects.filter(pk__in=purchases).delete()
        kept = 0
        for good in Good.all_objects.filter(pk__in=[int(pk) for pk in added]):
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
            if mine and not seller.purchases.filter(payment=Payment.CASH).exists():
                sellers.update_seller(actor=None, seller=seller, accepts_cash=False)
                self.stdout.write(f"  {seller.name}: cash switched off again.")
        self.stdout.write(
            f"Removed {len(purchases)} cash sales."
            + (f" Kept {kept} new good(s) sold since." if kept else "")
        )


def _at(day, at) -> datetime:
    return timezone.make_aware(datetime.combine(day, at))
