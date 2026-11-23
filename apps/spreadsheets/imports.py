"""Excel imports: developers, employee numbers, cards (with assignment), balances and goods.

Every row goes through the same validation and services as the web forms (audit log
included). All or nothing: if any row has an error nothing is saved, and every error is
reported with its Excel row number. `dry_run` checks a file the same way without saving.
"""

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from apps.audit.services import record_audit
from apps.cash import services as cash
from apps.developers import services as developers
from apps.developers.models import Developer, DeveloperStatus
from apps.developers.serializers import DeveloperSerializer
from apps.finance import services as finance
from apps.goods import services as goods
from apps.goods.models import Good, GoodKind, MovementKind
from apps.goods.serializers import GoodSerializer
from apps.rfid import services as rfid
from apps.rfid.models import (
    Building,
    RFIDCard,
    RFIDCardAssignment,
    label_taken,
    normalize_uid,
    uid_validator,
)
from apps.rfid.scope import building_scope
from apps.sellers.access import GOODS, acting_positions, acting_seller
from apps.sellers.models import Seller, ServicePosition
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
    # Optional: without it, the row labels the card the developer holds now.
    Column("card_uid", _("Card UID"), width=14, example="04DE00AB12"),
    Column("card_label", _("Card number"), width=12, example="0001"),
    Column("employee_number", _("Employee no."), width=14, example="E1001"),
]
RENUMBER_COLUMNS = [
    Column("old_employee_number", _("Current employee no."), True, 18, example="E1001"),
    Column("new_employee_number", _("New employee no."), True, 18, example="E2001"),
]
BALANCE_COLUMNS = [
    EMPLOYEE,
    Column("amount", _("Amount"), True, 12, kind="money", example="100.00"),
    Column("description", _("Description"), width=30, example="Opening balance"),
]
# The goods export's Goods sheet uses the same columns, so it can be edited and imported back.
GOODS_COLUMNS = [
    Column("seller", _("Seller"), width=20, example="Cafe"),
    Column("counter", _("Counter"), width=18, example="Counter 1"),
    Column("good", _("Good"), True, 24, example="Green tea"),
    Column("sku", _("SKU"), width=12, example="TEA-01"),
    Column("kind", _("Kind"), width=10, example="PRODUCT"),
    Column("price", _("Price"), width=10, kind="money", example="2.50"),
    Column("description", _("Description"), width=30),
    Column("track_stock", _("Track stock"), width=11, kind="bool", example=True),
    Column("decimal_quantity", _("Decimal quantities"), width=11, kind="bool", example=False),
    Column("quantity", _("In stock"), width=10, kind="quantity", example=20),
    Column("is_active", _("For sale"), width=9, kind="bool", example=True),
]


def _label_free(label: str, card) -> None:
    """A card label is unique: another card with it stops the row."""
    taken = label_taken(label, card.pk if card else None)
    if taken is not None:
        raise RowError(
            _("This card number is already used by card %(uid)s.") % {"uid": taken.uid},
            "card_label",
        )


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
    rows: list = field(default_factory=list)  # every (number, row) of the file
    pending: list = field(default_factory=list)  # work left for Importer.finish


class Importer:
    title = ""
    columns: list[Column] = []
    permissions: list[str] = []
    excel_permission = ""  # the import right for this kind, e.g. excel.import_cards

    def apply(self, ctx: Context, number: int, row: dict) -> str:
        """Save one row; return "created", "updated" or "unchanged"."""
        raise NotImplementedError

    def finish(self, ctx: Context) -> None:
        """Called once after every row passed (also on a dry run): for work on the whole file."""

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
    excel_permission = "excel.import_developers"
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


class RenumberImporter(Importer):
    """Gives developers new employee numbers: one row per developer, current number and new
    number. Numbers may move along a chain or swap (E1 -> E2 and E2 -> E1), since a number is
    checked against the developers left after the whole file. Cards, balances and history
    follow the developer, not the number."""

    title = _("Change employee numbers")
    columns = RENUMBER_COLUMNS
    permissions = ["developer.update"]
    excel_permission = "excel.import_developers"
    MAX_LENGTH = Developer._meta.get_field("employee_number").max_length

    def apply(self, ctx, number, row):
        old = text(row.get("old_employee_number"))
        new = text(row.get("new_employee_number"))
        if not old:
            raise RowError(_("This field is required."), "old_employee_number")
        if not new:
            raise RowError(_("This field is required."), "new_employee_number")
        self.once(ctx, "old_employee_number", old)
        self.once(ctx, "new_employee_number", new)
        if not ctx.actor.has_rbac_perm("developer.update"):
            raise RowError(_("You may not do this."))
        developer = Developer.objects.filter(employee_number=old).first()
        if developer is None:
            raise RowError(_("No developer with this employee number."), "old_employee_number")
        scope = building_scope(ctx.actor, "developer.update")
        if scope is not None and developer.building_id not in scope:
            raise RowError(_("This developer is not in your buildings."), "old_employee_number")
        if new == old:
            return "unchanged"
        if len(new) > self.MAX_LENGTH:
            raise RowError(
                _("Ensure this field has no more than {max_length} characters.").format(
                    max_length=self.MAX_LENGTH
                ),
                "new_employee_number",
            )
        # Free if nobody has it, or its holder gets a new number in this file too.
        olds = {text(r.get("old_employee_number")) for _n, r in ctx.rows}
        if new not in olds and Developer.objects.filter(employee_number=new).exists():
            raise RowError(_("This employee number is already in use."), "new_employee_number")
        ctx.pending.append((developer, new))
        return "updated"

    def finish(self, ctx):
        # Two steps, so a swap or chain never meets the unique number on the way.
        now = timezone.now()
        for developer, _new in ctx.pending:
            Developer.objects.filter(pk=developer.pk).update(
                employee_number=f"~renumber-{developer.pk}", updated_at=now
            )
        for developer, new in ctx.pending:
            Developer.objects.filter(pk=developer.pk).update(employee_number=new, updated_at=now)
            record_audit(
                "developer.updated",
                actor=ctx.actor,
                entity=developer,
                old_values={"employee_number": developer.employee_number},
                new_values={"employee_number": new},
            )


class CardImporter(Importer):
    """New UIDs are registered; a label updates the card's label; an employee number
    assigns the card to that developer (if it isn't theirs already).

    Without a UID, a row (employee number + label) puts the label on the card that
    developer holds now: for a list of who has which printed card number."""

    title = _("Cards")
    columns = CARD_COLUMNS
    permissions = ["card.assign"]
    excel_permission = "excel.import_cards"

    def apply(self, ctx, number, row):
        if not text(row.get("card_uid")):
            return self._label_current_card(ctx, row)
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
        if label:
            self.once(ctx, "card_label", label)
            _label_free(label, card)
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

    def _label_current_card(self, ctx, row):
        employee = text(row.get("employee_number"))
        label = text(row.get("card_label"))
        if not employee or not label:
            raise RowError(
                _("Enter the card UID, or the employee number and the card number."), "card_uid"
            )
        self.once(ctx, "employee_number", employee)
        self.once(ctx, "card_label", label)
        developer = _developer(ctx, employee, "card.assign")
        current = (
            RFIDCardAssignment.objects.filter(developer=developer, unassigned_at__isnull=True)
            .select_related("card")
            .first()
        )
        if current is None:
            raise RowError(
                _("This developer has no card yet: assign one first, or enter its UID."),
                "employee_number",
            )
        card = current.card
        if card.label == label:
            return "unchanged"
        _label_free(label, card)
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
        return "updated"


class BalanceImporter(Importer):
    """One deposit per row. The same employee, amount and description is deposited only
    once, so importing a file again (even re-saved or re-ordered) adds nothing."""

    title = _("Opening balances")
    columns = BALANCE_COLUMNS
    permissions = ["finance.deposit"]
    excel_permission = "excel.import_balances"

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


YES = {"true", "yes", "y", "1", "예", "o"}
NO = {"false", "no", "n", "0", "아니", "아니오", "x"}


def yes_no(value, key: str) -> bool | None:
    """An Excel TRUE/FALSE, yes/no or 1/0 cell; None when blank."""
    if value is None or text(value) == "":
        return None
    if isinstance(value, bool):
        return value
    word = text(value).lower()
    if word in YES:
        return True
    if word in NO:
        return False
    raise RowError(_("Enter yes or no."), key)


class GoodsImporter(Importer):
    """Goods of a store's counters (not courts). A row matches a good of the same store by
    SKU, else by name on the same counter: it is updated, otherwise created. On an update a
    blank cell leaves that field as it is. "In stock" is the count now (a stock-take): the
    difference is recorded as a stock adjustment. Rows go through the goods form's checks.

    Staff with goods rights import for any store (in their buildings); a store's own login
    for its own counters only."""

    title = _("Goods")
    columns = GOODS_COLUMNS
    permissions = ["good.create", "good.update"]
    excel_permission = "excel.import_goods"
    seller_area = GOODS  # a goods seller imports their own store's goods

    def _staff(self, ctx, permission: str) -> bool:
        return ctx.actor.has_rbac_perm(permission)

    def _position(self, ctx, row) -> ServicePosition:
        actor = ctx.actor
        staff = self._staff(ctx, "good.create") or self._staff(ctx, "good.update")
        own = None if staff else acting_seller(actor)
        seller_name = text(row.get("seller"))
        if own is not None:
            if seller_name and seller_name.lower() != own.name.lower():
                raise RowError(_("You can only import goods of your own store."), "seller")
            seller = own
        else:
            if not seller_name:
                raise RowError(_("This field is required."), "seller")
            seller = Seller.objects.filter(name__iexact=seller_name).first()
            if seller is None:
                raise RowError(_("No store with this name."), "seller")
        positions = ServicePosition.objects.filter(seller=seller).order_by("id")
        mine = None if staff else acting_positions(actor)
        if mine is not None:
            positions = positions.filter(pk__in=mine)
        counter = text(row.get("counter"))
        if counter:
            position = positions.filter(name__iexact=counter).first()
            if position is None:
                raise RowError(_("No counter with this name in this store."), "counter")
        else:
            found = list(positions.filter(is_active=True)[:2])
            if len(found) != 1:
                raise RowError(_("Name the counter: this store has several."), "counter")
            position = found[0]
        if staff:
            scope = building_scope(actor, "good.view")
            if scope is not None and position.building_id not in scope:
                raise RowError(_("This counter is not in your buildings."), "counter")
        return position

    def _existing(self, position, name: str, sku: str) -> Good | None:
        if sku:
            match = Good.objects.filter(
                sku__iexact=sku, service_position__seller_id=position.seller_id
            ).first()
            if match is not None:
                return match
        return Good.objects.filter(service_position=position, name__iexact=name).first()

    def _context(self, ctx) -> dict:
        actor = ctx.actor
        staff = self._staff(ctx, "good.create") or self._staff(ctx, "good.update")
        return {
            "own_seller": None if staff else acting_seller(actor),
            "own_positions": None if staff else acting_positions(actor),
        }

    def apply(self, ctx, number, row):
        name = text(row.get("good"))
        sku = text(row.get("sku"))
        position = self._position(ctx, row)
        self.once(ctx, "good", f"{position.seller_id}|{(sku or name).lower()}")
        good = self._existing(position, name, sku)
        kind = text(row.get("kind")).upper()
        if good is not None and good.kind == GoodKind.RENTAL:
            return "unchanged"  # courts are managed on the Playground page
        if kind == GoodKind.RENTAL:
            raise RowError(_("Courts are added on the Playground page, not imported."), "kind")

        data = {}
        if name:
            data["name"] = name
        if sku:
            data["sku"] = sku
        if kind:
            data["kind"] = kind
        price = text(row.get("price"))
        if price:
            data["price"] = price
        description = text(row.get("description"))
        if description:
            data["description"] = description
        for key in ("track_stock", "decimal_quantity", "is_active"):
            value = yes_no(row.get(key), key)
            if value is not None:
                data[key] = value
        stock = text(row.get("quantity"))
        quantity = None
        if stock:
            # Whole units unless the good allows decimals (the serializer and
            # move_stock check that); at most 2 decimals.
            try:
                quantity = Decimal(stock)
            except (InvalidOperation, ValueError) as exc:
                raise RowError(_("Enter a number, 0 or more."), "quantity") from exc
            if quantity < 0 or quantity != quantity.quantize(Decimal("0.01")):
                raise RowError(_("Enter a number, 0 or more, with at most 2 decimals."), "quantity")

        if good is None:
            if (
                not ctx.actor.has_rbac_perm("good.create")
                and self._context(ctx)["own_seller"] is None
            ):
                raise RowError(_("Your account cannot add goods."), "good")
            if "price" not in data:
                raise RowError(_("This field is required."), "price")
            data["service_position"] = position.pk
            if quantity:
                data["initial_quantity"] = quantity
            serializer = GoodSerializer(data=data, context=self._context(ctx))
            serializer.is_valid(raise_exception=True)
            cash.create_store_good(actor=ctx.actor, **serializer.validated_data)
            return "created"

        if not ctx.actor.has_rbac_perm("good.update") and self._context(ctx)["own_seller"] is None:
            raise RowError(_("Your account cannot change goods."), "good")
        if data.get("kind") == good.kind:
            del data["kind"]  # the same kind is no change (a different one is refused)
        serializer = GoodSerializer(good, data=data, partial=True, context=self._context(ctx))
        serializer.is_valid(raise_exception=True)
        changed = {k: v for k, v in serializer.validated_data.items() if getattr(good, k) != v}
        if changed:
            good = cash.update_store_good(actor=ctx.actor, good=good, **changed)
        moved = False
        if quantity is not None:
            if not good.track_stock:
                if quantity:
                    raise RowError(_("This good does not track stock."), "quantity")
            elif quantity != good.quantity:
                cash.ensure_may_change_stock(ctx.actor, good, MovementKind.ADJUSTMENT)
                goods.move_stock(
                    good=good,
                    kind=MovementKind.ADJUSTMENT,
                    delta=quantity - good.quantity,
                    actor=ctx.actor,
                    reason="Excel import (stock count)",
                )
                moved = True
        return "updated" if changed or moved else "unchanged"


IMPORTERS = {
    "developers": DeveloperImporter(),
    "renumber": RenumberImporter(),
    "cards": CardImporter(),
    "balances": BalanceImporter(),
    "goods": GoodsImporter(),
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
    ctx = Context(actor=actor, seen=set(), rows=rows)
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
        if not errors:
            importer.finish(ctx)
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
