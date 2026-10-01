import random
from datetime import datetime, time, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.attendance.models import AttendanceRecord
from apps.attendance.services import rebuild
from apps.developers.models import DeveloperStatus
from apps.rfid.models import (
    DevicePurpose,
    RFIDCardAssignment,
    RFIDDevice,
    RFIDEvent,
    ScanResult,
)


class Command(BaseCommand):
    help = (
        "Demo data: about a month of realistic door scans (Door1/Door2) for the demo "
        "developers, up to yesterday, then rebuild attendance and presence from them. "
        "Days that already have attendance for a developer are skipped."
    )

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30)

    def handle(self, *args, days: int, **options):
        if not settings.DEBUG:
            raise CommandError("seed_attendance_month only runs with DEBUG=True.")
        if settings.ATTENDANCE_DIRECTION_RULE != "device":
            raise CommandError(
                "Set ATTENDANCE_DIRECTION_RULE=device (the doors report in/out) before seeding."
            )
        doors = list(
            RFIDDevice.objects.filter(
                purpose=DevicePurpose.ATTENDANCE, code__in=["Door1", "Door2"], is_active=True
            ).order_by("code")
        )
        if len(doors) != 2:
            raise CommandError("Door1 and Door2 must exist (run seed_demo).")

        rng = random.Random(2026)
        today = timezone.localdate()
        first, last = today - timedelta(days=days), today - timedelta(days=1)
        assignments = list(
            RFIDCardAssignment.objects.filter(
                unassigned_at__isnull=True,
                card__status="ACTIVE",
                developer__employee_number__startswith="DEMO-",
                developer__deleted_at__isnull=True,
            ).select_related("card", "developer")
        )
        existing = set(
            AttendanceRecord.objects.filter(work_date__range=(first, last)).values_list(
                "developer_id", "work_date"
            )
        )

        events = []
        for a in assignments:
            dev, card = a.developer, a.card
            home = doors[dev.pk % 2]
            other = doors[1 - dev.pk % 2]
            for offset in range(days):
                day = first + timedelta(days=offset)
                if (dev.pk, day) in existing:
                    continue
                if dev.status == DeveloperStatus.ON_LEAVE or (
                    dev.start_date and day < dev.start_date
                ):
                    continue
                for moment, door, direction in _day_pattern(rng, day, home, other):
                    events.append(
                        RFIDEvent(
                            device=door,
                            client_event_id=f"demo-month-{dev.pk}-{day:%Y%m%d}-{len(events)}",
                            uid=card.uid,
                            card=card,
                            developer=dev,
                            event_time=moment,
                            received_at=moment,
                            direction=direction,
                            result=ScanResult.ACCEPTED,
                        )
                    )
        with transaction.atomic():
            RFIDEvent.objects.bulk_create(events, batch_size=2000)
        self.stdout.write(f"Created {len(events)} door scans from {first} to {last}.")

        result = rebuild(date_from=first, date_to=last)
        self.stdout.write(self.style.SUCCESS(f"Rebuilt attendance: {result}"))


def _at(day, hour, minute=0):
    return timezone.make_aware(datetime.combine(day, time(hour, minute)))


def _day_pattern(rng, day, home, other):
    """One developer's door scans for one day: (moment, door, direction)."""
    weekday = day.weekday()
    if weekday == 6 or (weekday == 5 and rng.random() > 0.05):
        return []  # Sunday off; a few people come in on Saturdays
    if weekday < 5 and rng.random() < 0.08:
        return []  # absent

    scans = []
    if weekday == 5:  # short Saturday visit
        t = _at(day, 10) + timedelta(minutes=rng.randint(0, 90))
        scans.append((t, home, "IN"))
        scans.append((t + timedelta(hours=rng.randint(2, 4)), home, "OUT"))
        return scans

    t = _at(day, 7, 30) + timedelta(minutes=rng.randint(0, 135))
    door = home
    scans.append((t, door, "IN"))
    if rng.random() < 0.40:  # lunch outside
        out = _at(day, 12) + timedelta(minutes=rng.randint(0, 60))
        scans.append((out, door, "OUT"))
        scans.append((out + timedelta(minutes=rng.randint(30, 60)), door, "IN"))
    if rng.random() < 0.10:  # walk over to the other building
        move = _at(day, 14) + timedelta(minutes=rng.randint(0, 90))
        scans.append((move, door, "OUT"))
        door = other
        scans.append((move + timedelta(minutes=rng.randint(2, 6)), door, "IN"))
    if rng.random() >= 0.03:  # 3% forget to scan out
        leave = _at(day, 16, 30) + timedelta(minutes=rng.randint(0, 180))
        scans.append((leave, door, "OUT"))
    return scans
