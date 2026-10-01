from datetime import date

from django.core.management.base import BaseCommand

from apps.attendance.services import rebuild


class Command(BaseCommand):
    help = (
        "Create attendance for accepted scans that have none and recompute daily summaries. "
        "Run after changing ATTENDANCE_DIRECTION_RULE or ATTENDANCE_DAY_START_HOUR. "
        "Also rebuilds who is inside which building."
    )

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="date_from", type=date.fromisoformat)
        parser.add_argument("--to", dest="date_to", type=date.fromisoformat)

    def handle(self, *args, date_from=None, date_to=None, **options):
        result = rebuild(date_from=date_from, date_to=date_to)
        self.stdout.write(self.style.SUCCESS(", ".join(f"{k}: {v}" for k, v in result.items())))
