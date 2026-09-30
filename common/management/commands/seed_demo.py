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
from apps.rfid import services as rfid
from apps.rfid.models import RFIDCard

DEPARTMENTS = ["Engineering", "Research", "Platform", "Design", "QA"]
FIRST = ["Ada", "Alan", "Grace", "Linus", "Margaret", "Dennis", "Barbara", "Ken", "Radia", "Tim"]
LAST = ["Lovelace", "Turing", "Hopper", "Torvalds", "Hamilton", "Ritchie", "Liskov", "Thompson"]


class Command(BaseCommand):
    help = "Fill a development database with demo users, developers, cards, readers and scans."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("seed_demo only runs with DEBUG=True.")
        if Developer.all_objects.filter(employee_number__startswith="DEMO-").exists():
            raise CommandError("Demo data already exists.")
        # Random per run: the repository is public, so a fixed demo password would let
        # anyone log in to any server seeded with it.
        password = secrets.token_urlsafe(12)
        random.seed(42)
        with transaction.atomic():
            self._users(password)
            developers = self._developers()
            self._cards(developers)
            keys = self._devices()
        self._scans(developers)

        self.stdout.write(self.style.SUCCESS("Demo data created."))
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
                email=f"dev{i}@demo.local",
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
