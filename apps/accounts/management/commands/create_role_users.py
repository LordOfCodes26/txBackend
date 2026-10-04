import getpass
import re
import secrets

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.models import USERNAME_PATTERN, Role, User, UserRole
from apps.accounts.rbac import ROLES

USERNAME_RE = re.compile(USERNAME_PATTERN)


class Command(BaseCommand):
    help = (
        "Create one login per role, named after the role (admin, boss, finance_manager, ...; "
        "with --prefix chonha_: chonha_admin, ...). Existing users are skipped. Each new "
        "user gets a random password, printed once "
        "(or one password you type, with --ask-password)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--prefix", default="", help="Put this before each username, e.g. chonha_ (optional)."
        )
        parser.add_argument(
            "--roles", nargs="+", metavar="ROLE", help="Only these roles (default: all)."
        )
        parser.add_argument(
            "--ask-password",
            action="store_true",
            help="Type one password for all new users instead of random ones.",
        )

    def handle(self, *args, prefix: str = "", roles=None, ask_password=False, **options):
        prefix = prefix.strip().lower()
        if prefix and not USERNAME_RE.fullmatch(prefix + "x" * 3):
            raise CommandError(f"Not a valid username prefix: {prefix!r}")
        codes = [c.upper() for c in roles] if roles else list(ROLES)
        unknown = [c for c in codes if not Role.objects.filter(code=c).exists()]
        if unknown:
            raise CommandError(f"Unknown roles: {', '.join(unknown)} (run migrate first?)")

        shared = self._ask_password() if ask_password else None
        created = []
        with transaction.atomic():
            for code in codes:
                username = f"{prefix}{code.lower()}"
                if User.objects.filter(username__iexact=username).exists():
                    self.stdout.write(f"  {username:30} exists, skipped")
                    continue
                password = shared or secrets.token_urlsafe(12)
                user = User.objects.create_user(
                    username=username, password=password, full_name=code.replace("_", " ").title()
                )
                UserRole.objects.create(user=user, role=Role.objects.get(code=code))
                created.append((username, code, password))

        if not created:
            self.stdout.write("No new users.")
            return
        self.stdout.write(self.style.SUCCESS(f"Created {len(created)} users:"))
        for username, code, password in created:
            shown = "(the password you typed)" if shared else password
            self.stdout.write(f"  {username:30} {code:17} {shown}")
        if not shared:
            self.stdout.write(
                self.style.WARNING(
                    "Passwords are shown only now: store them safely. Users change theirs with "
                    "POST /api/v1/auth/password/ (or: manage.py changepassword <username>)."
                )
            )

    def _ask_password(self) -> str:
        password = getpass.getpass("Password for the new users: ")
        if password != getpass.getpass("Again: "):
            raise CommandError("The passwords don't match.")
        try:
            validate_password(password)
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from exc
        return password
