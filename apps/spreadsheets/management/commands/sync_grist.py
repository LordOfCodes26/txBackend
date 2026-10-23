from django.core.management.base import BaseCommand

from apps.spreadsheets.grist import GristError, SyncBusy, configured, run_sync


class Command(BaseCommand):
    help = (
        "Copy developers, cards, goods, transactions and purchases into the Grist document "
        "(GRIST_URL). Does nothing when Grist is not set up. Exit code 1 if the sync failed."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--full", action="store_true", help="Compare every table in full (else once a day)."
        )

    def handle(self, *args, full=False, **options):
        if not configured():
            self.stdout.write("Grist is not set up (GRIST_URL, GRIST_PASSWORD); nothing to do.")
            return
        try:
            status = run_sync(full=full)
        except SyncBusy as exc:
            self.stdout.write(str(exc))
            return
        except GristError as exc:
            self.stdout.write(self.style.ERROR(str(exc)))
            raise SystemExit(1) from exc
        for table, counts in status["tables"].items():
            self.stdout.write(
                f"{table}: {counts['added']} added, {counts['changed']} changed, "
                f"{counts['removed']} removed"
            )
        if not status["ok"]:
            self.stdout.write(self.style.ERROR(f"Grist sync failed: {status['error']}"))
            raise SystemExit(1)
        self.stdout.write(
            self.style.SUCCESS("Grist sync done" + (" (full)." if status["full"] else "."))
        )
