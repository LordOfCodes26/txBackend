import random
import secrets
import uuid
from datetime import date, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.test import override_settings
from django.utils import timezone

from apps.developers.models import Developer, DeveloperStatus
from apps.finance import services as finance
from apps.finance.models import DeveloperAccount
from apps.goods import services as goods
from apps.goods.models import GoodKind
from apps.purchases import services as purchases
from apps.purchases.models import Purchase
from apps.rfid import services as rfid
from apps.rfid.models import RFIDCard, RFIDCardAssignment, RFIDEvent
from apps.sellers.models import Seller, ServicePosition
from common import demo_devices

FIRST = [
    "Ada",
    "Alan",
    "Grace",
    "Linus",
    "Margaret",
    "Dennis",
    "Barbara",
    "Ken",
    "Radia",
    "Tim",
    "Edsger",
    "Donald",
    "Frances",
    "John",
    "Katherine",
    "Guido",
    "Bjarne",
    "Anita",
    "Hedy",
    "Claude",
    "Sophie",
    "Niklaus",
    "Jean",
    "Mary",
    "Adele",
]
LAST = [
    "Lovelace",
    "Turing",
    "Hopper",
    "Torvalds",
    "Hamilton",
    "Ritchie",
    "Liskov",
    "Thompson",
    "Perlman",
    "Berners-Lee",
    "Dijkstra",
    "Knuth",
    "Allen",
    "McCarthy",
    "Johnson",
    "Rossum",
    "Stroustrup",
    "Borg",
    "Lamarr",
    "Shannon",
    "Wilson",
    "Wirth",
    "Sammet",
    "Keller",
]
DEPARTMENTS = ["Engineering", "Research", "Platform", "Design", "QA", "Data", "Security"]


def _demo_pin() -> str:
    while True:
        pin = f"{secrets.randbelow(10_000):04d}"
        steps = {int(b) - int(a) for a, b in zip(pin, pin[1:], strict=False)}
        if len(set(pin)) > 1 and steps not in ({1}, {-1}):
            return pin


class Command(BaseCommand):
    help = (
        "Add a larger demo data set on top of seed_demo: 200 more developers with cards, "
        "balances and a shared demo PIN, a bakery, till readers with serial numbers for every "
        "counter, today's door scans and past purchases. Each section runs only once."
    )

    def add_arguments(self, parser):
        parser.add_argument("--developers", type=int, default=200)

    def handle(self, *args, developers: int, **options):
        if not settings.DEBUG:
            raise CommandError("seed_more only runs with DEBUG=True.")
        if not Developer.all_objects.filter(employee_number__startswith="DEMO-").exists():
            raise CommandError("Run seed_demo first.")
        random.seed(7)
        self.pin = None

        if not Developer.all_objects.filter(employee_number="DEMO-0031").exists():
            self._developers(developers)
        self._pins()
        if not Seller.objects.filter(name="Demo Bakery").exists():
            self._bakery()
        self._readers()
        self._door_scans()
        if Purchase.objects.filter(status="CONFIRMED").count() < 20:
            self._purchases(30)

        self.stdout.write(self.style.SUCCESS("Done."))
        if self.pin:
            self.stdout.write(f"Demo developers' purchase PIN (shown only now): {self.pin}")

    # -- people ---------------------------------------------------------------------------

    def _developers(self, count: int):
        start = Developer.all_objects.filter(employee_number__startswith="DEMO-").count() + 1
        leads = {}
        today = timezone.localdate()
        buildings = list(demo_devices.ensure_buildings().values())
        with transaction.atomic():
            for i in range(start, start + count):
                dept = random.choice(DEPARTMENTS)
                dev = Developer.objects.create(
                    employee_number=f"DEMO-{i:04d}",
                    full_name=f"{random.choice(FIRST)} {random.choice(LAST)}",
                    phone=f"+1 555 {i:04d}",
                    home_address=f"{i} Sample Avenue, Demo City",
                    birthday=date(1975 + i % 25, 1 + i % 12, 1 + i % 28),
                    department=dept,
                    position_title="Team lead" if dept not in leads else "Developer",
                    start_date=today - timedelta(days=random.randint(20, 3000)),
                    status=DeveloperStatus.ON_LEAVE if i % 37 == 0 else DeveloperStatus.ACTIVE,
                    building=buildings[i % len(buildings)],
                )
                leads.setdefault(dept, dev)
                finance.open_account(dev)
                if i % 41 != 0:  # a few developers have no card yet
                    card = RFIDCard.objects.create(uid=f"04DF{i:06X}", label=f"{i:04d}")
                    RFIDCardAssignment.objects.create(card=card, developer=dev)
        for dev in Developer.objects.filter(employee_number__gte=f"DEMO-{start:04d}"):
            finance.deposit(
                actor=None,
                developer=dev,
                amount=random.choice(["30.00", "60.00", "100.00", "150.00", "250.00"]),
                description="Monthly allowance",
                idempotency_key=f"demo-more-dep-{dev.pk}",
            )
        self.stdout.write(f"Created {count} developers with cards and balances.")

    def _pins(self):
        missing = DeveloperAccount.objects.filter(
            developer__employee_number__startswith="DEMO-", pin_hash=""
        )
        if not missing.exists():
            return
        accounts = list(missing)
        self.pin = _demo_pin()
        for account in accounts:
            finance.set_pin(actor=None, account=account, pin=self.pin, current_pin=None)
        self.stdout.write(f"Set the demo PIN for {len(accounts)} developer accounts.")

    # -- sellers and devices -------------------------------------------------------------

    def _bakery(self):
        with transaction.atomic():
            bakery = Seller.objects.create(name="Demo Bakery", contact_name="Baker")
            counter = ServicePosition.objects.create(
                seller=bakery, name="Bakery counter", location="Building 2, ground floor"
            )
            for name, price, kind, qty in [
                ("Sourdough loaf", "4.50", GoodKind.PRODUCT, 30),
                ("Cinnamon roll", "2.20", GoodKind.PRODUCT, 60),
                ("Chocolate cookie", "1.30", GoodKind.PRODUCT, 120),
                ("Cheesecake slice", "3.80", GoodKind.PRODUCT, 24),
                ("Espresso", "1.80", GoodKind.SERVICE, 0),
                ("Cappuccino", "2.90", GoodKind.SERVICE, 0),
            ]:
                goods.create_good(
                    actor=None,
                    service_position=counter,
                    name=name,
                    price=price,
                    kind=kind,
                    track_stock=kind == GoodKind.PRODUCT,
                    initial_quantity=qty,
                )
        self.stdout.write("Created Demo Bakery with 6 goods.")

    def _readers(self):
        """Till readers Reader1-4 of the demo sellers, and the card assign readers."""
        created = demo_devices.ensure_readers()
        if created:
            self.stdout.write(f"Readers: {', '.join(created)}")

    # -- activity ------------------------------------------------------------------------

    def _door_scans(self):
        """Today's door scans: in at a way-in unit, out at a way-out unit of the
        developer's building (some walk over to the other building)."""
        doors = demo_devices.doors_by_building()
        if len(doors) < 2:
            self.stdout.write(self.style.WARNING("Door units missing; skipping door scans."))
            return
        since = timezone.now() - timedelta(hours=4)
        if RFIDEvent.objects.filter(
            received_at__gte=since, client_event_id__startswith="demo-door-"
        ).exists():
            return
        assignments = list(
            RFIDCardAssignment.objects.filter(
                unassigned_at__isnull=True, card__status="ACTIVE", developer__building__isnull=False
            )
            .select_related("card", "developer")
            .order_by("?")[:180]
        )
        now = timezone.now()
        scans = []
        for a in assignments:
            home = doors.get(a.developer.building_id) or next(iter(doors.values()))
            other = next((d for b, d in doors.items() if d is not home), home)
            arrive = now - timedelta(minutes=random.randint(30, 230))
            scans.append((arrive, random.choice(home["IN"]), a.card, "IN"))
            roll = random.random()
            if roll < 0.15:  # went home again
                t = arrive + timedelta(minutes=random.randint(10, 25))
                scans.append((t, random.choice(home["OUT"]), a.card, "OUT"))
            elif roll < 0.30:  # moved to the other building
                t = arrive + timedelta(minutes=random.randint(10, 25))
                scans.append((t, random.choice(home["OUT"]), a.card, "OUT"))
                scans.append((t + timedelta(minutes=2), random.choice(other["IN"]), a.card, "IN"))
        for i, (moment, door, card, direction) in enumerate(sorted(scans, key=lambda s: s[0])):
            rfid.record_scan(
                device=door,
                uid=card.uid,
                event_time=moment,
                direction=direction,
                client_event_id=f"demo-door-{now:%Y%m%d}-{i:05d}",
            )
        self.stdout.write(
            f"Recorded {len(scans)} door scans for {len(assignments)} developers today."
        )

    def _purchases(self, count: int):
        positions = [
            p
            for p in ServicePosition.objects.filter(is_active=True, seller__status="ACTIVE")
            if p.goods.exclude(kind=GoodKind.RENTAL).filter(is_active=True).exists()
        ]
        cards = list(
            RFIDCard.objects.filter(
                status="ACTIVE",
                assignments__unassigned_at__isnull=True,
                assignments__developer__account__balance__gte=20,
                assignments__developer__employee_number__startswith="DEMO-",
                assignments__developer__status=DeveloperStatus.ACTIVE,
            ).exclude(assignments__developer__account__pin_hash="")[:200]
        )
        if not positions or not cards or not self.pin:
            self.stdout.write(
                self.style.WARNING(
                    "Skipping purchases (needs counters, cards and a freshly set demo PIN)."
                )
            )
            return
        done = 0
        with override_settings(PURCHASE_ALLOW_MANUAL_CARD_UID=True):
            for _ in range(count):
                position = random.choice(positions)
                seller_user = position.seller.user
                purchase = purchases.create_purchase(actor=seller_user, service_position=position)
                options = list(position.goods.exclude(kind=GoodKind.RENTAL).filter(is_active=True))
                for good in random.sample(options, k=min(len(options), random.randint(1, 3))):
                    if good.track_stock and good.quantity < 2:
                        continue
                    purchases.add_item(purchase=purchase, good=good, quantity=random.randint(1, 2))
                try:
                    purchases.confirm_purchase(
                        actor=seller_user,
                        purchase=purchase,
                        pin=self.pin,
                        idempotency_key=str(uuid.uuid4()),
                        card_uid=random.choice(cards).uid,
                    )
                    done += 1
                except Exception as exc:  # e.g. empty bucket or low balance: just skip it
                    purchases.cancel_purchase(actor=seller_user, purchase=purchase)
                    self.stdout.write(self.style.WARNING(f"  skipped a purchase: {exc}"))
        self.stdout.write(f"Created {done} confirmed purchases.")
