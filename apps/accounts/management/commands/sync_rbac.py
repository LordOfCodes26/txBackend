from django.core.management.base import BaseCommand

from apps.accounts.rbac import sync_rbac


class Command(BaseCommand):
    help = "Sync permissions and system roles from the RBAC catalog (runs after migrate too)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--prune", action="store_true", help="Delete permissions no longer in the catalog."
        )

    def handle(self, *args, prune=False, **options):
        result = sync_rbac(prune=prune)
        self.stdout.write(f"Created permissions: {result.created_permissions or 'none'}")
        self.stdout.write(f"Created roles: {result.created_roles or 'none'}")
        if result.stale_permissions:
            verb = "Deleted" if prune else "Stale (use --prune to delete)"
            self.stdout.write(self.style.WARNING(f"{verb}: {result.stale_permissions}"))
