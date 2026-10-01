import random
import secrets
from datetime import datetime, time, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Role, User, UserRole
from apps.accounts.rbac import ROLES
from apps.developers.models import Developer, DeveloperStatus
from apps.finance import services as finance
from apps.finance.models import AccountTransaction
from apps.goods import services as goods
from apps.rfid import services as rfid
from apps.rfid.models import RFIDCard
from apps.sellers.models import Seller, ServicePosition

DEPARTMENTS = ["Engineering", "Research", "Platform", "Design", "QA"]
FIRST = ["Ada", "Alan", "Grace", "Linus", "Margaret", "Dennis", "Barbara", "Ken", "Radia", "Tim"]
LAST = ["Lovelace", "Turing", "Hopper", "Torvalds", "Hamilton", "Ritchie", "Liskov", "Thompson"]


class Command(BaseCommand):
    help = "Fill a development database with demo users, developers, cards, readers and scans."

    def handle(self, *args, **options):
        """Each section is created once; re-running adds only sections added since."""
        if not settings.DEBUG:
            raise CommandError("seed_demo only runs with DEBUG=True.")
        random.seed(42)
        created = False
        if not Developer.all_objects.filter(employee_number__startswith="DEMO-").exists():
            self._people()
            created = True
        if not Seller.objects.filter(name__startswith="Demo ").exists():
            with transaction.atomic():
                self._catalog()
            self.stdout.write(self.style.SUCCESS("Demo sellers and goods created."))
            created = True
        if not Seller.objects.filter(name="Demo Sports Center").exists():
            with transaction.atomic():
                self._rentals()
            self.stdout.write(self.style.SUCCESS("Demo rentals created."))
            created = True
        if not rfid.RFIDDevice.objects.filter(code__in=["Door1", "Door2"]).exists():
            for code, building in (("Door1", "Building 1"), ("Door2", "Building 2")):
                _, door_key = rfid.register_device(
                    actor=None, code=code, name=f"{building} door", location=building
                )
                self.stdout.write(f"Door device {code} API key (shown only now): {door_key}")
            created = True
        if not rfid.RFIDDevice.objects.filter(purpose="TILL").exists():
            counter = ServicePosition.objects.filter(
                seller__name="Demo Cafe", name="Counter 1"
            ).first()
            if counter is not None:
                _, till_key = rfid.register_device(
                    actor=None,
                    code="TILL-CAFE-1",
                    name="Cafe counter 1 reader",
                    purpose="TILL",
                    service_position=counter,
                )
                self.stdout.write(self.style.SUCCESS("Demo till device created."))
                self.stdout.write(f"Till TILL-CAFE-1 API key (shown only now): {till_key}")
                created = True
        if not AccountTransaction.objects.filter(idempotency_key__startswith="demo-").exists():
            self._deposits()
            self.stdout.write(self.style.SUCCESS("Demo deposits created."))
            created = True
        if not created:
            raise CommandError("Demo data already exists.")

    def _people(self):
        # Random per run: the repository is public, so a fixed demo password would let
        # anyone log in to any server seeded with it.
        password = secrets.token_urlsafe(12)
        with transaction.atomic():
            self._users(password)
            developers = self._developers()
            self._cards(developers)
            keys = self._devices()
        self._scans(developers)

        self.stdout.write(self.style.SUCCESS("Demo people, cards and scans created."))
        self.stdout.write(f"Logins (password {password}; shown only now):")
        for code in ROLES:
            self.stdout.write(f"  {code.lower()}@demo.local  ({code})")
        for code, key in keys.items():
            self.stdout.write(f"Reader {code} API key: {key}")

    def _users(self, password):
        for code in ROLES:
            user = User.objects.create_user(
                email=f"{code.lower()}@demo.local",
                password=password,
                full_name=code.replace("_", " ").title(),
            )
            UserRole.objects.create(user=user, role=Role.objects.get(code=code))

    def _developers(self):
        developers = []
        leads = {}
        for i in range(1, 31):
            dept = DEPARTMENTS[i % len(DEPARTMENTS)]
            name = f"{random.choice(FIRST)} {random.choice(LAST)}"
            dev = Developer.objects.create(
                employee_number=f"DEMO-{i:04d}",
                full_name=name,
                phone=f"+1 555 01{i:02d}",
                home_address=f"{i} Demo Street, Demo City",
                birthday=timezone.localdate().replace(year=1985 + i % 15, day=1)
                - timedelta(days=random.randint(0, 300)),
                department=dept,
                position_title="Team lead" if dept not in leads else "Developer",
                manager=leads.get(dept),
                start_date=timezone.localdate() - timedelta(days=random.randint(30, 2000)),
                status=DeveloperStatus.ON_LEAVE if i == 7 else DeveloperStatus.ACTIVE,
            )
            leads.setdefault(dept, dev)
            developers.append(dev)
        developer_user = User.objects.get(email="developer@demo.local")
        developers[0].user = developer_user
        developers[0].save(update_fields=["user"])
        return developers

    def _cards(self, developers):
        for i, dev in enumerate(developers[:27], start=1):
            card = rfid.register_card(actor=None, uid=f"04DE{i:06X}", label=f"{i:04d}")
            rfid.assign_card(actor=None, card=card, developer=dev)
        rfid.change_card_status(
            actor=None,
            card=RFIDCard.objects.get(label="0005"),
            transition="block",
            reason="Reported lost",
        )
        for i in range(28, 33):
            rfid.register_card(actor=None, uid=f"04DE{i:06X}", label=f"{i:04d}")

    def _devices(self):
        keys = {}
        for code, location in [("READER-001", "Main entrance"), ("READER-002", "Back entrance")]:
            _, keys[code] = rfid.register_device(actor=None, code=code, location=location)
        return keys

    def _scans(self, developers):
        devices = list(rfid.RFIDDevice.objects.all())
        today = timezone.localdate()
        tz = timezone.get_current_timezone()
        for days_ago in range(5, -1, -1):
            day = today - timedelta(days=days_ago)
            if day.weekday() >= 5:
                continue
            for dev in developers[:27]:
                card = dev.card_assignments.get(unassigned_at__isnull=True).card
                arrive = datetime.combine(day, time(8, 30), tz) + timedelta(
                    minutes=random.randint(0, 90)
                )
                leave = arrive + timedelta(hours=8, minutes=random.randint(0, 60))
                for moment in (arrive, arrive + timedelta(seconds=2), leave):
                    if moment < timezone.now():
                        rfid.record_scan(
                            device=random.choice(devices), uid=card.uid, event_time=moment
                        )
            rfid.record_scan(
                device=devices[0],
                uid="04BADBAD01",
                event_time=datetime.combine(day, time(9, 0), tz),
            )

    def _catalog(self):
        cafe = Seller.objects.create(
            name="Demo Cafe",
            contact_name="Cafe Owner",
            user=User.objects.filter(email="seller@demo.local").first(),
        )
        shop = Seller.objects.create(name="Demo Tech Shop", contact_name="Shop Owner")
        counter = ServicePosition.objects.create(seller=cafe, name="Counter 1", location="Lobby")
        kiosk = ServicePosition.objects.create(seller=cafe, name="Kiosk", location="3rd floor")
        store = ServicePosition.objects.create(seller=shop, name="Store", location="Ground floor")
        items = [
            (counter, "Americano", "2.50", 0, False),
            (counter, "Latte", "3.20", 0, False),
            (counter, "Croissant", "2.80", 40, True),
            (kiosk, "Sparkling water", "1.20", 120, True),
            (kiosk, "Chocolate bar", "1.50", 0, True),
            (store, "USB-C cable", "9.90", 25, True),
            (store, "Mechanical keyboard", "89.00", 6, True),
            (store, "Laptop stand", "34.50", 10, True),
        ]
        for position, name, price, qty, tracked in items:
            goods.create_good(
                actor=None,
                service_position=position,
                name=name,
                price=price,
                track_stock=tracked,
                initial_quantity=qty,
            )

    def _deposits(self):
        developers = Developer.objects.filter(employee_number__startswith="DEMO-")
        for dev in developers:
            finance.deposit(
                actor=None,
                developer=dev,
                amount=random.choice(["50.00", "100.00", "150.00", "200.00"]),
                description="Monthly allowance",
                idempotency_key=f"demo-deposit-{dev.pk}",
            )

    def _rentals(self):
        from datetime import time

        center = Seller.objects.create(name="Demo Sports Center", contact_name="Center Desk")
        outdoor = ServicePosition.objects.create(seller=center, name="Outdoor", location="Garden")
        indoor = ServicePosition.objects.create(seller=center, name="Indoor", location="Basement")
        goods.create_good(
            actor=None,
            service_position=outdoor,
            name="Playground",
            kind="RENTAL",
            price="20.00",
            description="Whole playground, exclusive use.",
            rental={
                "slot_minutes": 60,
                "opening_time": time(8),
                "closing_time": time(20),
                "max_slots_per_booking": 3,
                "max_days_ahead": 14,
            },
        )
        goods.create_good(
            actor=None,
            service_position=indoor,
            name="Pool",
            kind="RENTAL",
            price="15.00",
            description="Private pool session.",
            rental={
                "slot_minutes": 30,
                "opening_time": time(9),
                "closing_time": time(21),
                "weekdays": [0, 1, 2, 3, 4, 5],
                "max_slots_per_booking": 4,
                "max_days_ahead": 30,
            },
        )
