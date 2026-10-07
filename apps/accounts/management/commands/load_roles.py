"""Set roles from a JSON file made by export_roles: each listed role gets exactly those
permissions (missing roles are created). The installer runs it once, on a first install,
with deploy/roles.json. The Admin role always keeps every permission."""

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.models import Permission, Role
from apps.accounts.rbac import ALL, ROLES, Roles, sync_rbac
from apps.audit.services import record_audit


class Command(BaseCommand):
    help = "Set roles and their permissions from a JSON file (see export_roles)."

    def add_arguments(self, parser):
        parser.add_argument("file")

    @transaction.atomic
    def handle(self, *args, file, **options):
        try:
            with open(file, encoding="utf-8") as handle:
                roles = json.load(handle)["roles"]
        except (OSError, ValueError, KeyError) as exc:
            raise CommandError(f"Cannot read roles from {file}: {exc}") from exc
        sync_rbac()  # every permission in the catalog exists first
        known = {p.codename: p for p in Permission.objects.all()}
        for spec in roles:
            code = spec["code"]
            wanted = set(spec.get("permissions", []))
            if code == Roles.ADMIN:
                wanted = set(ALL)
            unknown = sorted(wanted - known.keys())
            if unknown:
                self.stderr.write(f"{code}: skipping unknown permissions {', '.join(unknown)}")
            role, created = Role.objects.get_or_create(
                code=code,
                defaults={"name": spec.get("name", code), "is_system": code in ROLES},
            )
            role.name = spec.get("name", role.name)
            role.description = spec.get("description", role.description)
            role.save()
            role.permissions.set([known[c] for c in sorted(wanted & known.keys())])
            record_audit(
                "role.created" if created else "role.updated",
                actor=None,
                entity=role,
                new_values={"permissions": sorted(wanted & known.keys()), "source": file},
            )
            self.stdout.write(f"{code}: {len(wanted & known.keys())} permissions")
        self.stdout.write(self.style.SUCCESS(f"Set {len(roles)} roles from {file}."))
