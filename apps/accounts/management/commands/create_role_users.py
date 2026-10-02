import getpass
import secrets

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.models import Role, User, UserRole
from apps.accounts.rbac import ROLES


class Command(BaseCommand):
    help = (
        "Create one login per role: <role>@<domain>, e.g. admin@chonha.com with the ADMIN "
        "role. Existing users are skipped. Each new user gets a random password, printed once "
        "(or one password you type, with --ask-password)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--domain", required=True, help="e.g. chonha.com")
        parser.add_argument(
            "--roles", nargs="+", metavar="ROLE", help="Only these roles (default: all)."
        )
        parser.add_argument(
            "--ask-password",
            action="store_true",
            help="Type one password for all new users instead of random ones.",
        )

    def handle(self, *args, domain: str, roles=None, ask_password=False, **options):
        domain = domain.strip().lstrip("@").lower()
        if not domain or "." not in domain or "@" in domain:
            raise CommandError(f"Not a valid email domain: {domain!r}")
        codes = [c.upper() for c in roles] if roles else list(ROLES)
        unknown = [c for c in codes if not Role.objects.filter(code=c).exists()]
        if unknown:
            raise CommandError(f"Unknown roles: {', '.join(unknown)} (run migrate first?)")

        shared = self._ask_password() if ask_password else None
        created = []
        with transaction.atomic():
            for code in codes:
                email = f"{code.lower()}@{domain}"
                if User.objects.filter(email__iexact=email).exists():
                    self.stdout.write(f"  {email:40} exists, skipped")
                    continue
                password = shared or secrets.token_urlsafe(12)
                user = User.objects.create_user(
                    email=email, password=password, full_name=code.replace("_", " ").title()
                )
                UserRole.objects.create(user=user, role=Role.objects.get(code=code))
                created.append((email, code, password))

        if not created:
            self.stdout.write("No new users.")
            return
        self.stdout.write(self.style.SUCCESS(f"Created {len(created)} users:"))
        for email, code, password in created:
            shown = "(the password you typed)" if shared else password
            self.stdout.write(f"  {email:40} {code:17} {shown}")
        if not shared:
            self.stdout.write(
                self.style.WARNING(
                    "Passwords are shown only now: store them safely. Users change theirs with "
                    "POST /api/v1/auth/password/ (or: manage.py changepassword <email>)."
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
