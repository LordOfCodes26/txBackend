from django.core.management.base import BaseCommand, CommandError

from common import data_reset


class Command(BaseCommand):
    help = (
        "Delete all data except users, roles, RFID readers, buildings, sellers, counters and "
        "goods (developers, cards, scans, TCP log, attendance, money, purchases, stock "
        "history, audit log). Makes a database backup first. Asks you to type "
        f'"{data_reset.CONFIRM_PHRASE}" unless --confirm gives it.'
    )

    def add_arguments(self, parser):
        parser.add_argument("--confirm", help=f'the phrase "{data_reset.CONFIRM_PHRASE}"')

    def handle(self, *args, confirm=None, **options):
        info = data_reset.summary()
        self.stdout.write("Deletes:")
        for key, n in info["delete"].items():
            self.stdout.write(f"  {key:20} {n:>8}")
        self.stdout.write("Keeps:")
        for key, n in info["keep"].items():
            self.stdout.write(f"  {key:20} {n:>8}")
        if confirm is None:
            try:
                confirm = input(f'\nType "{data_reset.CONFIRM_PHRASE}" to continue: ')
            except EOFError:
                confirm = ""
        if confirm != data_reset.CONFIRM_PHRASE:
            raise CommandError("Not confirmed: nothing was deleted.")
        self.stdout.write(f"Backing up the database to {data_reset.backup_dir()} ...")
        try:
            result = data_reset.reset_data(actor=None, confirm=confirm)
        except data_reset.BackupFailed as exc:
            raise CommandError(f"{exc.detail} {exc.details or ''}") from exc
        self.stdout.write(f"Backup: {result['backup']}")
        total = sum(result["deleted"].values())
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {total} rows. Opening stock kept for {result['opening_stock']} goods."
            )
        )
