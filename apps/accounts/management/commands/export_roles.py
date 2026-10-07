"""Write every role (name, description, permissions) to a JSON file for load_roles."""

import json

from django.core.management.base import BaseCommand

from apps.accounts.models import Role


class Command(BaseCommand):
    help = "Export roles and their permissions to JSON (stdout, or --output FILE)."

    def add_arguments(self, parser):
        parser.add_argument("--output", help="File to write; default: print it.")

    def handle(self, *args, output=None, **options):
        roles = [
            {
                "code": role.code,
                "name": role.name,
                "description": role.description,
                "permissions": sorted(role.permissions.values_list("codename", flat=True)),
            }
            for role in Role.objects.prefetch_related("permissions").order_by("code")
        ]
        text = json.dumps({"roles": roles}, indent=2, ensure_ascii=False) + "\n"
        if output:
            with open(output, "w", encoding="utf-8") as handle:
                handle.write(text)
            self.stdout.write(f"Wrote {len(roles)} roles to {output}.")
        else:
            self.stdout.write(text, ending="")
