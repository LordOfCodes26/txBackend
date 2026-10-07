"""Excel imports: developers, cards (with assignment) and opening balances.

Every row goes through the same validation and services as the web forms (audit log
included). All or nothing: if any row has an error nothing is saved, and every error is
reported with its Excel row number. `dry_run` checks a file the same way without saving.
"""

import hashlib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from apps.audit.services import record_audit
from apps.developers import services as developers
from apps.developers.models import Developer, DeveloperStatus
from apps.developers.serializers import DeveloperSerializer
from apps.finance import services as finance
from apps.rfid import services as rfid
from apps.rfid.models import Building, RFIDCard, RFIDCardAssignment, normalize_uid, uid_validator
from apps.rfid.scope import building_scope
from common.exceptions import DomainError

from .xlsx import BadWorkbook, Column, as_date, read_rows

EMPLOYEE = Column("employee_number", _("Employee no."), True, 14, example="E1001")
DEVELOPER_COLUMNS = [
    EMPLOYEE,
    Column("full_name", _("Full name"), True, 24, example="Kim Chol"),
    Column("phone", _("Phone"), width=16, example="+850 2 123 4567"),
    Column("home_address", _("Home address"), width=30),
    Column("birthday", _("Birthday"), width=12, kind="date", example="1990-05-01"),
    Column("department", _("Department"), width=16, example="Engineering"),
    Column("position_title", _("Position"), width=16, example="Developer"),
    Column("building", _("Building"), width=12, example="B1"),
    Column("start_date", _("Start date"), width=12, kind="date", example="2026-10-01"),
    Column("out_date", _("Last working day"), width=14, kind="date"),
    Column("status", _("Status"), width=12, example="ACTIVE"),
]
CARD_COLUMNS = [
    Column("card_uid", _("Card UID"), True, 14, example="04DE00AB12"),
    Column("card_label", _("Card label"), width=12, example="0001"),
    Column("employee_number", _("Employee no."), width=14, example="E1001"),
]
BALANCE_COLUMNS = [
    EMPLOYEE,
    Column("amount", _("Amount"), True, 12, kind="money", example="100.00"),
    Column("description", _("Description"), width=30, example="Opening balance"),
]


class RowError(Exception):
    def __init__(self, message, column: str | None = None):
        super().__init__(str(message))
        self.message = str(message)
        self.column = column


def text(value) -> str:
    """A cell as text: 1001.0 (a number typed in Excel) becomes "1001"."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


@dataclass
class Context:
    actor: object
    seen: set


class Importer:
    title = ""
    columns: list[Column] = []
    permissions: list[str] = []

    def apply(self, ctx: Context, number: int, row: dict) -> str:
        """Save one row; return "created", "updated" or "unchanged"."""
        raise NotImplementedError

    def label(self, key: str) -> str:
        for column in self.columns:
            if column.key == key:
                return str(column.label)
        return key

    def once(self, ctx: Context, key: str, value: str) -> None:
        if (key, value) in ctx.seen:
            raise RowError(_("This value appears twice in the file."), key)
        ctx.seen.add((key, value))


def _developer(ctx, employee_number: str, permission: str) -> Developer:
    developer = Developer.objects.filter(employee_number=employee_number).first()
    if developer is None:
        raise RowError(_("No developer with this employee number."), "employee_number")
    scope = building_scope(ctx.actor, permission)
    if scope is not None and developer.building_id not in scope:
        raise RowError(_("This developer is not in your buildings."), "employee_number")
    return developer


class DeveloperImporter(Importer):
    """New employee numbers are created; existing ones are updated. On an update a blank
    cell leaves that field as it is."""

    title = _("Developers")
    columns = DEVELOPER_COLUMNS
    permissions = ["developer.create", "developer.update"]
    STATUSES = {s.value: s.value for s in DeveloperStatus} | {
        s.label.upper(): s.value for s in DeveloperStatus
    }

    def apply(self, ctx, number, row):
        employee = text(row.get("employee_number"))
        self.once(ctx, "employee_number", employee)
        existing = Developer.objects.filter(employee_number=employee).first()
        data = {}
        for column in self.columns:
            if column.key not in row:
                continue
            value = row[column.key]
            if existing and value is None:
                continue  # blank: unchanged
            if column.kind == "date":
                data[column.key] = as_date(value)
            elif column.key == "building":
                data["building"] = self._building(value)
            elif column.key == "status":
                data["status"] = self._status(value)
            else:
                data[column.key] = text(value)
        data["employee_number"] = employee

        permission = "developer.update" if existing else "developer.create"
        if not ctx.actor.has_rbac_perm(permission):
            raise RowError(_("You may not do this."))
        scope = building_scope(ctx.actor, permission)
        if scope is not None:
            if existing and existing.building_id not in scope:
                raise RowError(_("This developer is not in your buildings."), "employee_number")
            building = data.get("building", existing.building_id if existing else None)
            if building not in scope:
                raise RowError(_("Choose one of your buildings."), "building")

        serializer = DeveloperSerializer(instance=existing, data=data, partial=bool(existing))
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        if existing is None:
            developers.create_developer(actor=ctx.actor, **values)
            return "created"
        changes = {k: v for k, v in values.items() if getattr(existing, k) != v}
        if not changes:
            return "unchanged"
        developers.update_developer(actor=ctx.actor, developer=existing, **changes)
        return "updated"

    def _building(self, value):
        if value is None:
            return None
        name = text(value)
        building = (
            Building.objects.filter(code__iexact=name).first()
            or Building.objects.filter(name__iexact=name).first()
        )
        if building is None:
            raise RowError(_("No building with this code or name."), "building")
        return building.pk

    def _status(self, value):
        if value is None:
            return DeveloperStatus.ACTIVE
        key = text(value).upper().replace(" ", "_")
        status = self.STATUSES.get(key) or self.STATUSES.get(text(value).upper())
        if status is None:
            raise RowError(
                _("Use one of: %(values)s.") % {"values": ", ".join(DeveloperStatus.values)},
                "status",
            )
        return status


class CardImporter(Importer):
    """New UIDs are registered; a label updates the card's label; an employee number
    assigns the card to that developer (if it isn't theirs already)."""

    title = _("Cards")
    columns = CARD_COLUMNS
    permissions = ["card.assign"]

    def apply(self, ctx, number, row):
        uid = normalize_uid(text(row.get("card_uid")))
        try:
            uid_validator(uid)
        except Exception as exc:
            raise RowError(_("UID must be 4-32 hexadecimal characters."), "card_uid") from exc
        self.once(ctx, "card_uid", uid)
        label = text(row.get("card_label"))
        employee = text(row.get("employee_number"))
        outcome = "unchanged"

        card = RFIDCard.objects.filter(uid=uid).first()
        if card is None:
            card = rfid.register_card(actor=ctx.actor, uid=uid, label=label)
            outcome = "created"
        elif label and card.label != label:
            old = card.label
            card.label = label
            card.save(update_fields=["label", "updated_at"])
            record_audit(
                "rfid.card_updated",
                actor=ctx.actor,
                entity=card,
                old_values={"label": old},
                new_values={"label": label},
            )
            outcome = "updated"

        if employee:
            self.once(ctx, "employee_number", employee)
            developer = _developer(ctx, employee, "card.assign")
            current = RFIDCardAssignment.objects.filter(card=card, unassigned_at__isnull=True)
            if not current.filter(developer=developer).exists():
                rfid.assign_card(actor=ctx.actor, card=card, developer=developer)
                outcome = "created" if outcome == "created" else "updated"
        return outcome


class BalanceImporter(Importer):
    """One deposit per row. The same employee, amount and description is deposited only
    once, so importing a file again (even re-saved or re-ordered) adds nothing."""

    title = _("Opening balances")
    columns = BALANCE_COLUMNS
    permissions = ["finance.deposit"]

    def apply(self, ctx, number, row):
        employee = text(row.get("employee_number"))
        self.once(ctx, "employee_number", employee)
        try:
            amount = Decimal(text(row.get("amount")))
        except InvalidOperation as exc:
            raise RowError(_("Enter an amount, e.g. 100.00."), "amount") from exc
        if amount <= 0:
            raise RowError(_("The amount must be more than 0."), "amount")
        developer = _developer(ctx, employee, "finance.deposit")
        description = text(row.get("description")) or "Opening balance"
        content = f"{developer.pk}|{finance.money(amount)}|{description}"
        _txn, created = finance.deposit(
            actor=ctx.actor,
            developer=developer,
            amount=amount,
            description=description,
            idempotency_key=f"import-{hashlib.sha256(content.encode()).hexdigest()[:24]}",
        )
        return "created" if created else "unchanged"


IMPORTERS = {
    "developers": DeveloperImporter(),
    "cards": CardImporter(),
    "balances": BalanceImporter(),
}


def _messages(importer: Importer, detail, column=None) -> list[tuple[str | None, str]]:
    """A DRF error detail as (column label, message) pairs."""
    if isinstance(detail, dict):
        out = []
        for key, value in detail.items():
            key = None if key == "non_field_errors" else key
            out += _messages(importer, value, key)
        return out
    if isinstance(detail, list):
        return [m for item in detail for m in _messages(importer, item, column)]
    return [(importer.label(column) if column else None, str(detail))]


def run_import(kind: str, upload, *, actor, dry_run: bool) -> dict:
    importer = IMPORTERS[kind]
    rows = read_rows(upload, importer.columns)
    if not rows:
        raise BadWorkbook(_("The file has no rows under the header."))
    ctx = Context(actor=actor, seen=set())
    counts = {"created": 0, "updated": 0, "unchanged": 0}
    errors = []
    with transaction.atomic():
        for number, row in rows:
            try:
                with transaction.atomic():
                    counts[importer.apply(ctx, number, row)] += 1
            except RowError as exc:
                column = importer.label(exc.column) if exc.column else None
                errors.append({"row": number, "column": column, "message": exc.message})
            except serializers.ValidationError as exc:
                for column, message in _messages(importer, exc.detail):
                    errors.append({"row": number, "column": column, "message": message})
            except DomainError as exc:
                errors.append({"row": number, "column": None, "message": str(exc.detail)})
        if errors or dry_run:
            transaction.set_rollback(True)
    return {
        "kind": kind,
        "rows": len(rows),
        **counts,
        "errors": errors,
        "dry_run": dry_run,
        "saved": not errors and not dry_run,
    }
