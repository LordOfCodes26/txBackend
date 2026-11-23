"""Copies the app's data into a Grist document, where people build their own tables and
reports on it (manage.py sync_grist, run every few minutes; "Sync now" on the Excel page).

One table per kind of record, matched on its App ID column: rows are added, changed and
removed so the table matches the app. Only the app's columns are written, so columns
people add in Grist stay, and a column's label can be renamed there (its id is untied from
the label). Rows people add by hand (no App ID) are left alone. Access rules keep everyone
but the sync from changing the copied columns or adding and deleting synced rows; App ID
stays out of the views (it is on the Raw Data page). Developers, cards and goods
are compared in full each time; transactions (append-only) and purchases (by when they
last changed) only from the newest row already in Grist, with a full check once a day.
"""

import base64
import json
import logging
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.db.models import Prefetch
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import override

from apps.developers.models import Developer
from apps.finance.models import AccountTransaction
from apps.goods.models import Good
from apps.purchases.models import Purchase, PurchaseItem, PurchaseStatus
from apps.rfid.models import RFIDCard, RFIDCardAssignment

log = logging.getLogger(__name__)

BATCH = 500  # records per Grist request
FULL_CHECK_EVERY = timedelta(hours=24)
LOCK_KEY = 0x6772697374  # Postgres advisory lock: one sync at a time
APP_ID = "App_ID"
SYNCED = (PurchaseStatus.CONFIRMED, PurchaseStatus.REFUNDED)  # not drafts or cancelled carts


class GristError(Exception):
    pass


class SyncBusy(GristError):
    pass


@dataclass(frozen=True)
class Col:
    id: str
    label: object  # lazy translation
    type: str = "Text"


def _date(value: date | None):
    """A Grist Date: seconds since 1970 at midnight UTC."""
    if value is None:
        return None
    return int(datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp())


def _moment(value: datetime | None):
    return value.timestamp() if value else None


def _money(value):
    return float(value) if value is not None else None


_quantity = _money  # whole or decimal (goods with decimal quantities)


# Statuses and kinds as words in GRIST_LANGUAGE (the app's codes otherwise).
LABELS = {
    "ACTIVE": _("Active"),
    "SUSPENDED": _("Suspended"),
    "TERMINATED": _("Terminated"),
    "FROZEN": _("Frozen"),
    "CLOSED": _("Closed"),
    "BLOCKED": _("Blocked"),
    "RETIRED": _("Retired"),
    "DEPOSIT": _("Deposit"),
    "PURCHASE": _("Purchase"),
    "REFUND": _("Refund"),
    "ADJUSTMENT": _("Manual adjustment"),
    "ECOMMERCE": _("E-commerce"),
    "CASH": _("Cash"),
    "CONFIRMED": _("Confirmed"),
    "REFUNDED": _("Refunded"),
    "SALE": _("Sale"),
    "BOOKING": _("Booking"),
    "PRODUCT": _("Product"),
    "SERVICE": _("Service"),
    "RENTAL": _("Court"),
}


def _label(code: str) -> str:
    return str(LABELS.get(code, code)) if code else ""


def _datetime_type() -> str:
    return f"DateTime:{settings.TIME_ZONE}"


# --- tables ---------------------------------------------------------------------------

DEVELOPERS = [
    Col("Employee_no", _("Employee no.")),
    Col("Full_name", _("Full name")),
    Col("Phone", _("Phone")),
    Col("Department", _("Department")),
    Col("Position", _("Position")),
    Col("Building", _("Building")),
    Col("Start_date", _("Start date"), "Date"),
    Col("Last_working_day", _("Last working day"), "Date"),
    Col("Status", _("Status")),
    Col("Card_UID", _("Card UID")),
    Col("Card_label", _("Card number")),
    Col("Balance", _("Balance"), "Numeric"),
]
CARDS = [
    Col("UID", _("Card UID")),
    Col("Label", _("Card number")),
    Col("Status", _("Status")),
    Col("Employee_no", _("Employee no.")),
    Col("Full_name", _("Full name")),
]
GOODS = [
    Col("Seller", _("Seller")),
    Col("Counter", _("Counter")),
    Col("Good", _("Good")),
    Col("SKU", _("SKU")),
    Col("Kind", _("Kind")),
    Col("Price", _("Price"), "Numeric"),
    Col("Track_stock", _("Track stock"), "Bool"),
    Col("Decimal_quantities", _("Decimal quantities"), "Bool"),
    Col("In_stock", _("In stock"), "Numeric"),
    Col("For_sale", _("For sale"), "Bool"),
]


def _transaction_columns():
    return [
        Col("Time", _("Time"), _datetime_type()),
        Col("Employee_no", _("Employee no.")),
        Col("Full_name", _("Full name")),
        Col("Kind", _("Kind")),
        Col("Deposit_type", _("Deposit type")),
        Col("Amount", _("Amount"), "Numeric"),
        Col("Balance_after", _("Balance after"), "Numeric"),
        Col("Description", _("Description")),
        Col("Reference", _("Reference")),
    ]


def _purchase_columns():
    return [
        Col("Time", _("Time"), _datetime_type()),
        Col("Purchase_no", _("Purchase no."), "Int"),
        Col("Seller", _("Seller")),
        Col("Counter", _("Counter")),
        Col("Kind", _("Kind")),
        Col("Status", _("Status")),
        Col("Payment", _("Payment")),
        Col("Employee_no", _("Employee no.")),
        Col("Full_name", _("Full name")),
        Col("Buyer_name", _("Buyer (cash)")),
        Col("Total", _("Total"), "Numeric"),
        Col("Refunded_at", _("Refunded at"), _datetime_type()),
        Col("Updated", _("Last changed"), _datetime_type()),
    ]


def _item_columns():
    return [
        Col("Purchase", _("Purchase no."), "Int"),  # the purchase's App ID
        Col("Time", _("Time"), _datetime_type()),
        Col("Seller", _("Seller")),
        Col("Good", _("Good")),
        Col("Quantity", _("Quantity"), "Numeric"),
        Col("Free_quantity", _("Free quantity"), "Int"),
        Col("Unit_price", _("Unit price"), "Numeric"),
        Col("Line_total", _("Line total"), "Numeric"),
    ]


def developer_rows() -> dict[int, dict]:
    cards = {
        a.developer_id: a.card
        for a in RFIDCardAssignment.objects.filter(unassigned_at__isnull=True).select_related(
            "card"
        )
    }
    rows = {}
    for d in Developer.objects.select_related("building", "account"):
        card = cards.get(d.pk)
        account = getattr(d, "account", None)
        rows[d.pk] = {
            "Employee_no": d.employee_number,
            "Full_name": d.full_name,
            "Phone": d.phone,
            "Department": d.department,
            "Position": d.position_title,
            "Building": d.building.name if d.building else "",
            "Start_date": _date(d.start_date),
            "Last_working_day": _date(d.out_date),
            "Status": _label(d.status),
            "Card_UID": card.uid if card else "",
            "Card_label": card.label if card else "",
            "Balance": _money(account.balance) if account else None,
        }
    return rows


def card_rows() -> dict[int, dict]:
    holders = {
        a.card_id: a.developer
        for a in RFIDCardAssignment.objects.filter(unassigned_at__isnull=True).select_related(
            "developer"
        )
    }
    rows = {}
    for c in RFIDCard.objects.all():
        holder = holders.get(c.pk)
        rows[c.pk] = {
            "UID": c.uid,
            "Label": c.label,
            "Status": _label(c.status),
            "Employee_no": holder.employee_number if holder else "",
            "Full_name": holder.full_name if holder else "",
        }
    return rows


def good_rows() -> dict[int, dict]:
    return {
        g.pk: {
            "Seller": g.service_position.seller.name,
            "Counter": g.service_position.name,
            "Good": g.name,
            "SKU": g.sku,
            "Kind": _label(g.kind),
            "Price": _money(g.price),
            "Track_stock": g.track_stock,
            "Decimal_quantities": g.decimal_quantity,
            "In_stock": _quantity(g.quantity),
            "For_sale": g.is_active,
        }
        for g in Good.objects.select_related("service_position__seller")
    }


def transaction_row(t) -> dict:
    developer = t.account.developer
    return {
        "Time": _moment(t.created_at),
        "Employee_no": developer.employee_number,
        "Full_name": developer.full_name,
        "Kind": _label(t.kind),
        "Deposit_type": _label(t.method),
        "Amount": _money(t.amount),
        "Balance_after": _money(t.balance_after),
        "Description": t.description,
        "Reference": t.reference,
    }


def purchase_row(p) -> dict:
    return {
        "Time": _moment(p.confirmed_at),
        "Purchase_no": p.store_number,
        "Seller": p.seller.name,
        "Counter": p.service_position.name if p.service_position else "",
        "Kind": _label(p.kind),
        "Status": _label(p.status),
        "Payment": str(_("Wallet")) if p.payment == "WALLET" else _label(p.payment),
        "Employee_no": p.developer.employee_number if p.developer else "",
        "Full_name": p.developer.full_name if p.developer else "",
        "Buyer_name": p.buyer_name,
        "Total": _money(p.total),
        "Refunded_at": _moment(p.refunded_at),
        "Updated": _moment(p.updated_at),
    }


def item_row(p, i) -> dict:
    return {
        "Purchase": p.pk,
        "Time": _moment(p.confirmed_at),
        "Seller": p.seller.name,
        "Good": i.good.name,
        "Quantity": _quantity(i.quantity),
        "Free_quantity": i.free_quantity,
        "Unit_price": _money(i.unit_price),
        "Line_total": _money(i.line_total),
    }


# --- Grist API ------------------------------------------------------------------------


class Client:
    """Grist's REST API, through the kit's login gate (HTTP Basic as the sync user)."""

    def __init__(self, url: str, user: str, password: str, timeout: int = 120):
        self.url = url.rstrip("/")
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.headers = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self.timeout = timeout

    def call(self, method: str, path: str, body=None, query: dict | None = None):
        url = self.url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers=self.headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode(errors="replace")
            raise GristError(f"{method} {path}: HTTP {exc.code} {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            raise GristError(f"Grist is not reachable at {self.url} ({reason})") from exc
        return json.loads(raw) if raw else None


class Sync:
    def __init__(self, client: Client, doc_name: str):
        self.client = client
        self.doc_name = doc_name
        self.doc = ""
        self.counts: dict[str, dict] = {}
        self.warnings: list[str] = []

    def api(self, method, path, body=None, query=None):
        return self.client.call(method, f"/api/docs/{self.doc}{path}", body, query)

    # Document and schema.

    def open_doc(self) -> bool:
        """Find the document by name, or make it. True when it was just made."""
        workspaces = self.client.call("GET", "/api/orgs/current/workspaces")
        for workspace in workspaces:
            for doc in workspace["docs"]:
                if doc["name"] == self.doc_name and not doc.get("removedAt"):
                    self.doc = doc["id"]
                    return False
        if not workspaces:
            raise GristError("The Grist team site has no workspace to put the document in.")
        self.doc = self.client.call(
            "POST", f"/api/workspaces/{workspaces[0]['id']}/docs", {"name": self.doc_name}
        )
        return True

    def ensure_table(self, table: str, columns: list[Col]) -> None:
        wanted = [{"id": c.id, "fields": {"label": str(c.label), "type": c.type}} for c in columns]
        wanted.append({"id": APP_ID, "fields": {"label": "App ID", "type": "Int"}})
        tables = {t["id"] for t in self.api("GET", "/tables")["tables"]}
        if table not in tables:
            self.api("POST", "/tables", {"tables": [{"id": table, "columns": wanted}]})
            have = {}
        else:
            have = {c["id"]: c for c in self.api("GET", f"/tables/{table}/columns")["columns"]}
            missing = [c for c in wanted if c["id"] not in have]
            if missing:
                self.api("POST", f"/tables/{table}/columns", {"columns": missing})
            # A column whose type the app changed (In stock: Int to Numeric).
            retype = [
                {"id": c["id"], "fields": {"type": c["fields"]["type"]}}
                for c in wanted
                if c["id"] in have and have[c["id"]]["fields"].get("type") != c["fields"]["type"]
            ]
            if retype:
                self.api("PATCH", f"/tables/{table}/columns", {"columns": retype})
        if APP_ID not in have:
            self.hide_app_id(table)
        # Untied, a label renamed in Grist keeps the id the sync writes to.
        untie = [
            {"id": c["id"], "fields": {"untieColIdFromLabel": True}}
            for c in wanted
            if not have.get(c["id"], {}).get("fields", {}).get("untieColIdFromLabel")
        ]
        if untie:
            self.api("PATCH", f"/tables/{table}/columns", {"columns": untie})

    def sql(self, query: str) -> list[dict]:
        return [r["fields"] for r in self.api("GET", "/sql", query={"q": query})["records"]]

    def hide_app_id(self, table: str) -> None:
        """App ID is for the sync only: out of the table's views, still on the Raw Data page.
        Once, when the column is made: someone may show it again."""
        fields = self.sql(
            "SELECT f.id FROM _grist_Views_section_field f"
            " JOIN _grist_Tables_column c ON f.colRef = c.id"
            " JOIN _grist_Tables t ON c.parentId = t.id"
            f" WHERE t.tableId = '{table}' AND c.colId = '{APP_ID}'"
            " AND f.parentId != t.rawViewSectionRef"
        )
        if fields:
            ids = [f["id"] for f in fields]
            self.api("POST", "/apply", [["BulkRemoveRecord", "_grist_Views_section_field", ids]])

    def lock(self, synced: dict[str, list[str]]) -> None:
        """Access rules, for everyone but the sync's own login: the copied columns can't be
        changed, nor synced rows (with an App ID) deleted or made. People's own columns, rows
        and tables stay theirs. Put back when missing or out of date."""
        email = self.client.call("GET", "/api/profile/user")["email"]
        others = f"user.Email != {json.dumps(email)}"
        memo = str(_("Copied from the app: change it there, or add a column of your own."))
        wanted = set()
        for table, columns in synced.items():
            wanted |= {
                (table, ",".join([*columns, APP_ID]), others, "-U"),
                (table, "*", f"{others} and rec.{APP_ID}", "-D"),
                (table, "*", f"{others} and newRec.{APP_ID}", "-C"),
            }
        resources = {
            (r["tableId"], r["colIds"]): r["id"]
            for r in self.sql("SELECT id, tableId, colIds FROM _grist_ACLResources")
        }
        rules = self.sql(
            "SELECT r.id, r.aclFormula, r.permissionsText, r.memo, r.rulePos, r.resource,"
            " s.tableId, s.colIds FROM _grist_ACLRules r"
            " JOIN _grist_ACLResources s ON r.resource = s.id"
        )
        ours = [r for r in rules if r["aclFormula"].startswith(others)]
        have = {(r["tableId"], r["colIds"], r["aclFormula"], r["permissionsText"]) for r in ours}
        if have == wanted and all(r["memo"] == memo for r in ours):
            return
        actions = []
        if ours:
            actions.append(["BulkRemoveRecord", "_grist_ACLRules", [r["id"] for r in ours]])
        # Resources left without rules (an older list of copied columns) go too.
        still_used = {r["resource"] for r in rules if r not in ours}
        needed = {(table, columns) for table, columns, _f, _p in wanted}
        unused = [
            rid
            for key, rid in resources.items()
            if key[0] in synced and key not in needed and rid not in still_used
        ]
        if unused:
            actions.append(["BulkRemoveRecord", "_grist_ACLResources", unused])
        new = sorted(needed - set(resources))
        for table, columns in new:
            actions.append(
                ["AddRecord", "_grist_ACLResources", None, {"tableId": table, "colIds": columns}]
            )
        if actions:
            made = self.api("POST", "/apply", actions)["retValues"][len(actions) - len(new) :]
            resources.update(zip(new, made, strict=True))
        start = max([r["rulePos"] or 0 for r in rules if r not in ours] + [0]) + 1
        self.api(
            "POST",
            "/apply",
            [
                [
                    "AddRecord",
                    "_grist_ACLRules",
                    None,
                    {
                        "resource": resources[(table, columns)],
                        "aclFormula": formula,
                        "permissionsText": permissions,
                        "rulePos": start + i,
                        "memo": memo,
                    },
                ]
                for i, (table, columns, formula, permissions) in enumerate(sorted(wanted))
            ],
        )

    def drop_starter_table(self) -> None:
        tables = {t["id"] for t in self.api("GET", "/tables")["tables"]}
        if "Table1" in tables and not self.api("GET", "/tables/Table1/records")["records"]:
            self.api("POST", "/apply", [["RemoveTable", "Table1"]])

    # Records.

    def records(self, table: str, app_ids: list[int] | None = None) -> list[dict]:
        if app_ids is None:
            return self.api("GET", f"/tables/{table}/records")["records"]
        out = []
        for start in range(0, len(app_ids), 200):  # keeps the URL short
            chunk = app_ids[start : start + 200]
            query = {"filter": json.dumps({APP_ID: chunk})}
            out += self.api("GET", f"/tables/{table}/records", query=query)["records"]
        return out

    def newest(self, table: str, column: str):
        query = {"sort": f"-{column}", "limit": 1}
        found = self.api("GET", f"/tables/{table}/records", query=query)["records"]
        return found[0]["fields"].get(column) if found else None

    def write(self, table: str, method: str, records: list[dict]) -> None:
        for start in range(0, len(records), BATCH):
            self.api(
                method, f"/tables/{table}/records", {"records": records[start : start + BATCH]}
            )

    def delete(self, table: str, row_ids: list[int]) -> None:
        for start in range(0, len(row_ids), BATCH):
            self.api("POST", f"/tables/{table}/data/delete", row_ids[start : start + BATCH])

    def reconcile(self, table, rows: dict[int, dict], existing: list[dict], remove=True) -> None:
        """Make `existing` (Grist records) match `rows` (App ID -> fields)."""
        by_app_id, extra = {}, []
        for record in existing:
            app_id = record["fields"].get(APP_ID)
            if app_id is None:
                continue  # added by hand in Grist
            if app_id in by_app_id:
                extra.append(record["id"])  # a copy: one row per record
            else:
                by_app_id[app_id] = record
        added, changed = [], []
        for app_id, fields in rows.items():
            record = by_app_id.get(app_id)
            if record is None:
                added.append({"fields": {**fields, APP_ID: app_id}})
                continue
            diff = {k: v for k, v in fields.items() if not _same(v, record["fields"].get(k))}
            if diff:
                changed.append({"id": record["id"], "fields": diff})
        removed = extra
        if remove:
            removed += [r["id"] for app_id, r in by_app_id.items() if app_id not in rows]
        self.write(table, "POST", added)
        # Grist wants the same fields in every record of a request.
        by_fields: dict[tuple, list] = {}
        for record in changed:
            by_fields.setdefault(tuple(sorted(record["fields"])), []).append(record)
        for group in by_fields.values():
            self.write(table, "PATCH", group)
        self.delete(table, removed)
        counts = self.counts.setdefault(table, {"added": 0, "changed": 0, "removed": 0})
        counts["added"] += len(added)
        counts["changed"] += len(changed)
        counts["removed"] += len(removed)

    def full(self, table: str, rows: dict[int, dict]) -> None:
        self.reconcile(table, rows, self.records(table))

    # The tables.

    def run(self, full: bool) -> None:
        if self.open_doc():
            full = True
        tables = {
            "Developers": DEVELOPERS,
            "Cards": CARDS,
            "Goods": GOODS,
            "Transactions": _transaction_columns(),
            "Purchases": _purchase_columns(),
            "Purchase_items": _item_columns(),
        }
        for table, columns in tables.items():
            self.ensure_table(table, columns)
            self.counts[table] = {"added": 0, "changed": 0, "removed": 0}
        self.drop_starter_table()
        try:
            self.lock({table: [c.id for c in columns] for table, columns in tables.items()})
        except GristError as exc:  # e.g. the sync's login doesn't own the document
            self.warnings.append(f"The copied columns could not be locked: {exc}")
            log.warning("Grist: %s", self.warnings[-1])
        self.full("Developers", developer_rows())
        self.full("Cards", card_rows())
        self.full("Goods", good_rows())
        self.sync_transactions(full)
        self.sync_purchases(full)

    def sync_transactions(self, full: bool) -> None:
        transactions = AccountTransaction.objects.select_related("account__developer").order_by(
            "id"
        )
        if full:
            rows = {t.pk: transaction_row(t) for t in transactions.iterator(chunk_size=2000)}
            self.full("Transactions", rows)
            return
        newest = self.newest("Transactions", APP_ID) or 0
        rows = {t.pk: transaction_row(t) for t in transactions.filter(pk__gt=newest)}
        self.reconcile("Transactions", rows, [])  # newer than any row there: only additions

    def sync_purchases(self, full: bool) -> None:
        purchases = (
            Purchase.objects.filter(status__in=SYNCED)
            .select_related("seller", "service_position", "developer")
            .prefetch_related(Prefetch("items", PurchaseItem.objects.select_related("good")))
            .order_by("id")
        )
        if not full:
            newest = self.newest("Purchases", "Updated")
            if newest is not None:
                # A second back: rows saved in the same second as the newest one.
                since = datetime.fromtimestamp(newest, tz=UTC) - timedelta(seconds=1)
                purchases = purchases.filter(updated_at__gte=since)
            else:
                full = True
        purchase_rows, item_rows = {}, {}
        for p in purchases:
            purchase_rows[p.pk] = purchase_row(p)
            for i in p.items.all():
                item_rows[i.pk] = item_row(p, i)
        if full:
            self.full("Purchases", purchase_rows)
            self.full("Purchase_items", item_rows)
            return
        ids = list(purchase_rows)
        self.reconcile("Purchases", purchase_rows, self.records("Purchases", ids))
        # The items of these purchases: changes can add, change and remove items.
        existing = []
        for start in range(0, len(ids), 200):
            query = {"filter": json.dumps({"Purchase": ids[start : start + 200]})}
            existing += self.api("GET", "/tables/Purchase_items/records", query=query)["records"]
        self.reconcile("Purchase_items", item_rows, existing)


def _same(a, b) -> bool:
    if a in (None, "") and b in (None, ""):
        return True
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 1e-6
        except (TypeError, ValueError):
            return False
    return a == b


# --- running it -----------------------------------------------------------------------


def configured() -> bool:
    return bool(settings.GRIST_URL and settings.GRIST_PASSWORD)


def doc_url(doc_id: str) -> str:
    base = settings.GRIST_PUBLIC_URL.rstrip("/")
    return f"{base}/doc/{doc_id}" if base and doc_id else ""


def read_status() -> dict:
    try:
        with open(settings.GRIST_STATUS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_status(status: dict) -> None:
    path = Path(settings.GRIST_STATUS_FILE)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".grist-status-")
        with os.fdopen(fd, "w") as f:
            json.dump(status, f)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except OSError:
        log.exception("Could not write the Grist status file %s", path)


def run_sync(full: bool = False, daily_full: bool = True) -> dict:
    """Sync now, unless another sync is running (SyncBusy). Returns the status it saved.

    `daily_full`: a full check when the last one is a day old (the timer; a web request
    leaves it to the timer, it can take longer than a request may)."""
    if not configured():
        raise GristError("Grist is not set up: set GRIST_URL and GRIST_PASSWORD.")
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_KEY])
        if not cursor.fetchone()[0]:
            raise SyncBusy("Another sync is running.")
    try:
        before = read_status()
        last_full = before.get("last_full_at")
        stale = (
            not last_full or timezone.now() - datetime.fromisoformat(last_full) > FULL_CHECK_EVERY
        )
        if daily_full and stale:
            full = True
        sync = Sync(
            Client(settings.GRIST_URL, settings.GRIST_USER, settings.GRIST_PASSWORD),
            settings.GRIST_DOC_NAME,
        )
        started = timezone.now()
        status = {"started_at": started.isoformat(), "full": full}
        try:
            with override(settings.GRIST_LANGUAGE):  # labels: new columns, statuses
                sync.run(full)
        except Exception as exc:
            status |= {"ok": False, "error": str(exc)[:500]}
            log.exception("Grist sync failed")
        else:
            status |= {"ok": True, "error": ""}
            if full:
                status["last_full_at"] = started.isoformat()
        status.setdefault("last_full_at", before.get("last_full_at"))
        status |= {
            "finished_at": timezone.now().isoformat(),
            "doc_id": sync.doc or before.get("doc_id", ""),
            "tables": sync.counts,
            "warnings": sync.warnings,
        }
        if status["ok"]:
            status["last_ok_at"] = status["finished_at"]
        else:
            status["last_ok_at"] = before.get("last_ok_at")
        _write_status(status)
        return status
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_KEY])
