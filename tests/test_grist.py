"""The Grist sync (apps/spreadsheets/grist.py), against an in-memory stand-in for Grist's API."""

import json
import re
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.accounts.rbac import Roles
from apps.developers import services as developers
from apps.developers.models import Developer
from apps.finance import services as finance
from apps.goods.models import Good
from apps.purchases.models import Purchase, PurchaseItem, PurchaseStatus
from apps.rfid.models import Building, RFIDCard, RFIDCardAssignment
from apps.sellers.models import Seller, ServicePosition
from apps.spreadsheets import grist

pytestmark = pytest.mark.django_db


class FakeGrist:
    """The parts of Grist's REST API the sync uses, kept in dicts."""

    def __init__(self):
        self.docs = {}
        self.down = False
        self.owner = True  # the sync's login may change access rules

    @staticmethod
    def _table(columns):
        return {"columns": {c["id"]: dict(c["fields"]) for c in columns}, "rows": {}, "next": 1}

    @staticmethod
    def _new_doc(name):
        return {
            "name": name,
            "tables": {"Table1": FakeGrist._table([])},
            # Metadata tables: id -> fields.
            "_grist_ACLResources": {1: {"tableId": "", "colIds": ""}},
            "_grist_ACLRules": {},
            "_grist_Views_section_field": {},  # a column shown in a view (raw: Raw Data)
        }

    @staticmethod
    def _show(doc, table, column):
        fields = doc["_grist_Views_section_field"]
        for raw in (True, False):
            fields[max(fields, default=0) + 1] = {"table": table, "col": column, "raw": raw}

    def _sql(self, doc, q):
        if "_grist_Views_section_field" in q:
            table = re.search(r"t.tableId = '(\w+)'", q)[1]
            fields = doc["_grist_Views_section_field"].items()
            return [
                {"id": i}
                for i, f in fields
                if f["table"] == table and f["col"] == "App_ID" and not f["raw"]
            ]
        resources = doc["_grist_ACLResources"]
        if "FROM _grist_ACLRules" in q:
            return [
                {"id": i, **rule, **resources[rule["resource"]]}
                for i, rule in doc["_grist_ACLRules"].items()
            ]
        return [{"id": i, **r} for i, r in resources.items()]

    def _apply(self, doc, actions):
        returned = []
        for action, table, *rest in actions:
            if action == "RemoveTable":
                del doc["tables"][table]
                returned.append(None)
                continue
            if table.startswith("_grist_ACL") and not self.owner:
                raise grist.GristError("POST /apply: HTTP 403 Only owners can change access rules")
            records = doc[table]
            if action == "BulkRemoveRecord":
                for row_id in rest[0]:
                    del records[row_id]
                returned.append(None)
            else:
                assert action == "AddRecord", action
                row_id = max(records, default=0) + 1
                records[row_id] = dict(rest[1])
                returned.append(row_id)
        return {"retValues": returned}

    def call(self, method, path, body=None, query=None):
        if self.down:
            raise grist.GristError("Grist is not reachable at http://grist.test (refused)")
        if path == "/api/orgs/current/workspaces":
            docs = [{"id": d, "name": doc["name"]} for d, doc in self.docs.items()]
            return [{"id": 3, "name": "Home", "docs": docs}]
        if path == "/api/workspaces/3/docs":
            doc_id = f"doc{len(self.docs) + 1}"
            self.docs[doc_id] = self._new_doc(body["name"])
            return doc_id
        if path == "/api/profile/user":
            return {"email": "app-sync@grist.local", "name": "app-sync"}
        doc_id, rest = re.fullmatch(r"/api/docs/(\w+)(/.*)", path).groups()
        doc = self.docs[doc_id]
        tables = doc["tables"]
        if rest == "/tables":
            if method == "GET":
                return {"tables": [{"id": t} for t in tables]}
            for spec in body["tables"]:
                tables[spec["id"]] = self._table(spec["columns"])
                for column in spec["columns"]:
                    self._show(doc, spec["id"], column["id"])
            return {"tables": [{"id": spec["id"]} for spec in body["tables"]]}
        if rest == "/apply":
            return self._apply(doc, body)
        if rest == "/sql":
            return {"records": [{"fields": f} for f in self._sql(doc, query["q"])]}
        name, what = re.fullmatch(r"/tables/(\w+)/(columns|records|data/delete)", rest).groups()
        table = tables[name]
        if what == "columns":
            if method == "GET":
                return {"columns": [{"id": c, "fields": f} for c, f in table["columns"].items()]}
            for column in body["columns"]:
                if method == "POST":
                    table["columns"][column["id"]] = dict(column["fields"])
                    self._show(doc, name, column["id"])
                else:
                    table["columns"][column["id"]].update(column["fields"])
            return None
        if what == "data/delete":
            for row_id in body:
                del table["rows"][row_id]
            return None
        if method == "GET":
            return {"records": self._select(table, query or {})}
        fields = {tuple(sorted(r["fields"])) for r in body["records"]}
        assert len(fields) == 1, "Grist: all records of a request need the same fields"
        for record in body["records"]:
            assert set(record["fields"]) <= set(table["columns"]), record
            if method == "POST":
                table["rows"][table["next"]] = dict(record["fields"])
                table["next"] += 1
            else:
                table["rows"][record["id"]].update(record["fields"])
        return None

    def _select(self, table, query):
        records = [
            {"id": i, "fields": {c: row.get(c) for c in table["columns"]}}
            for i, row in table["rows"].items()
        ]
        for column, values in json.loads(query.get("filter", "{}")).items():
            records = [r for r in records if r["fields"][column] in values]
        if "sort" in query:
            column = query["sort"].lstrip("-")
            records = [r for r in records if r["fields"][column] is not None]
            records.sort(key=lambda r: r["fields"][column], reverse=query["sort"][0] == "-")
        return records[: query["limit"]] if "limit" in query else records

    # For the tests.

    def doc(self):
        assert len(self.docs) == 1
        return next(iter(self.docs.values()))

    def rules(self) -> dict:
        """(table, columns, permissions) -> formula, of the access rules."""
        doc = self.doc()
        return {
            (r["tableId"], r["colIds"], r["permissionsText"]): r["aclFormula"]
            for r in self._sql(doc, "FROM _grist_ACLRules")
        }

    def rows(self, table) -> dict:
        """App ID -> fields."""
        rows = self.doc()["tables"][table]["rows"].values()
        return {r.get("App_ID"): r for r in rows}


@pytest.fixture
def fake(settings, tmp_path, monkeypatch):
    settings.GRIST_URL = "http://grist.test"
    settings.GRIST_PASSWORD = "secret"
    settings.GRIST_PUBLIC_URL = "https://grist.example/"
    settings.GRIST_STATUS_FILE = str(tmp_path / "grist-status.json")
    settings.TIME_ZONE = "UTC"
    server = FakeGrist()
    monkeypatch.setattr(grist, "Client", lambda *args, **kwargs: server)
    return server


@pytest.fixture
def data(admin):
    ada = developers.create_developer(
        actor=admin,
        employee_number="E1",
        full_name="Ada",
        start_date=timezone.localdate(),
        building=Building.objects.create(code="B1", name="North tower"),
    )
    bob = developers.create_developer(actor=admin, employee_number="E2", full_name="Bob")
    card = RFIDCard.objects.create(uid="04AA00BB11", label="0001")
    RFIDCardAssignment.objects.create(card=card, developer=ada)
    finance.deposit(
        actor=admin,
        developer=ada,
        amount=Decimal("50.00"),
        description="Opening",
        idempotency_key="k1",
    )
    seller = Seller.objects.create(name="Cafe")
    counter = ServicePosition.objects.create(seller=seller, name="Counter 1")
    tea = Good.objects.create(service_position=counter, name="Tea", price=Decimal("2.50"))
    cake = Good.objects.create(service_position=counter, name="Cake", price=Decimal("4.00"))
    purchase = Purchase.objects.create(
        seller=seller,
        service_position=counter,
        status=PurchaseStatus.CONFIRMED,
        developer=bob,
        total=Decimal("0"),  # all free: a confirmed purchase without a ledger entry
        confirmed_at=timezone.now(),
    )
    PurchaseItem.objects.create(
        purchase=purchase, good=tea, quantity=2, unit_price=Decimal("2.50"), line_total=0
    )
    Purchase.objects.create(seller=seller, service_position=counter)  # a draft: not synced
    return {"ada": ada, "bob": bob, "card": card, "purchase": purchase, "cake": cake}


def counts(status, table):
    c = status["tables"][table]
    return (c["added"], c["changed"], c["removed"])


def test_first_sync_makes_the_document_and_copies_everything(fake, data):
    status = grist.run_sync()
    assert (status["ok"], status["full"], status["error"]) == (True, True, "")
    tables = fake.doc()["tables"]
    assert fake.doc()["name"] == "Management data"
    assert set(tables) == {
        "Developers",
        "Cards",
        "Goods",
        "Transactions",
        "Purchases",
        "Purchase_items",
    }  # the new document's empty Table1 is gone
    assert all(f["untieColIdFromLabel"] for t in tables.values() for f in t["columns"].values())
    assert tables["Transactions"]["columns"]["Time"]["type"] == "DateTime:UTC"

    ada = fake.rows("Developers")[data["ada"].pk]
    assert (ada["Employee_no"], ada["Card_label"], ada["Balance"]) == ("E1", "0001", 50.0)
    assert (ada["Building"], ada["Status"]) == ("North tower", "Active")
    assert "Account_status" not in fake.doc()["tables"]["Developers"]["columns"]
    assert fake.rows("Developers")[data["bob"].pk]["Building"] == ""
    assert ada["Start_date"] % 86400 == 0  # a Grist date: midnight UTC
    assert fake.rows("Cards")[data["card"].pk]["Employee_no"] == "E1"
    (deposit,) = fake.rows("Transactions").values()
    assert (deposit["Amount"], deposit["Kind"]) == (50.0, "Deposit")
    assert list(fake.rows("Purchases")) == [data["purchase"].pk]  # not the draft
    (item,) = fake.rows("Purchase_items").values()
    assert (item["Purchase"], item["Good"], item["Quantity"]) == (data["purchase"].pk, "Tea", 2)
    assert status["doc_id"] == "doc1"
    assert grist.read_status()["last_ok_at"] == status["finished_at"]

    # App ID: on the Raw Data page only.
    shown = fake.doc()["_grist_Views_section_field"].values()
    assert {f["raw"] for f in shown if f["col"] == "App_ID"} == {True}
    assert any(not f["raw"] for f in shown if f["col"] == "Employee_no")
    # Locked for everyone but the sync: the copied columns, and synced rows.
    rules = fake.rules()
    others = 'user.Email != "app-sync@grist.local"'
    assert len(rules) == 18  # 3 per table
    assert rules[("Cards", "UID,Label,Status,Employee_no,Full_name,App_ID", "-U")] == others
    assert rules[("Goods", "*", "-D")] == f"{others} and rec.App_ID"
    assert rules[("Goods", "*", "-C")] == f"{others} and newRec.App_ID"
    assert status["warnings"] == []


def test_sync_again_sends_only_changes_and_keeps_what_people_added(fake, data, admin):
    grist.run_sync()
    again = grist.run_sync()
    assert again["full"] is False  # the full check is once a day
    assert all(sum(c.values()) == 0 for c in again["tables"].values())

    # People add a column and a row of their own, and copy a row.
    table = fake.doc()["tables"]["Developers"]
    table["columns"]["Notes"] = {"label": "Notes", "type": "Text"}
    ada_row = next(i for i, r in table["rows"].items() if r["App_ID"] == data["ada"].pk)
    table["rows"][ada_row]["Notes"] = "Team lead"
    table["rows"][99] = {"Full_name": "Someone typed in Grist"}
    table["rows"][100] = dict(table["rows"][ada_row])
    Developer.objects.filter(pk=data["ada"].pk).update(full_name="Ada Lovelace")
    developers.delete_developer(actor=admin, developer=data["bob"])

    status = grist.run_sync()
    assert counts(status, "Developers") == (0, 1, 2)  # Ada changed; Bob and the copy gone
    rows = table["rows"]
    assert rows[ada_row]["Full_name"] == "Ada Lovelace"
    assert rows[ada_row]["Notes"] == "Team lead"
    assert 99 in rows and 100 not in rows
    assert counts(status, "Purchases") == (0, 0, 0)  # Bob's purchase stays


def test_locks_are_put_back_and_follow_new_columns(fake, data, monkeypatch):
    grist.run_sync()
    doc = fake.doc()
    before = dict(doc["_grist_ACLRules"])
    grist.run_sync()
    assert doc["_grist_ACLRules"] == before  # nothing to do

    # An owner removes a rule; a newer app copies one more column.
    removed = next(i for i, r in before.items() if r["permissionsText"] == "-D")
    del doc["_grist_ACLRules"][removed]
    monkeypatch.setattr(grist, "CARDS", [*grist.CARDS, grist.Col("Notes", "Notes")])
    grist.run_sync()
    rules = fake.rules()
    assert len(rules) == 18
    assert ("Cards", "UID,Label,Status,Employee_no,Full_name,Notes,App_ID", "-U") in rules
    used = {r["resource"] for r in doc["_grist_ACLRules"].values()}
    assert set(doc["_grist_ACLResources"]) == used | {1}  # the old column list is gone


def test_without_owner_rights_the_sync_still_runs(fake, data):
    fake.owner = False
    status = grist.run_sync()
    assert status["ok"] is True
    assert "could not be locked" in status["warnings"][0]
    assert fake.rows("Developers")


def test_statuses_and_kinds_in_the_grist_language(fake, data, settings):
    settings.GRIST_LANGUAGE = "ko-kp"
    grist.run_sync()
    ada = fake.rows("Developers")[data["ada"].pk]
    assert ada["Status"] == "활성"
    assert [r["Kind"] for r in fake.rows("Transactions").values()] == ["충진"]
    assert fake.doc()["tables"]["Developers"]["columns"]["Employee_no"]["label"] == "직원번호"


def test_transactions_and_purchases_go_by_what_changed(fake, data, admin):
    grist.run_sync()
    finance.deposit(
        actor=admin,
        developer=data["bob"],
        amount=Decimal("5"),
        description="More",
        idempotency_key="k2",
    )
    purchase = data["purchase"]
    purchase.items.all().delete()
    PurchaseItem.objects.create(
        purchase=purchase, good=data["cake"], quantity=1, unit_price=Decimal("4"), line_total=0
    )
    purchase.status = PurchaseStatus.REFUNDED
    purchase.refunded_at = timezone.now()
    purchase.updated_at = timezone.now() + timedelta(seconds=5)  # later than the first sync
    Purchase.objects.filter(pk=purchase.pk).update(
        status=purchase.status, refunded_at=purchase.refunded_at, updated_at=purchase.updated_at
    )

    status = grist.run_sync()
    assert status["full"] is False
    assert counts(status, "Transactions") == (1, 0, 0)
    assert counts(status, "Purchases") == (0, 1, 0)
    assert counts(status, "Purchase_items") == (1, 0, 1)
    assert fake.rows("Purchases")[purchase.pk]["Status"] == "Refunded"
    assert [r["Good"] for r in fake.rows("Purchase_items").values()] == ["Cake"]


def test_full_check_once_a_day_puts_back_rows_removed_in_grist(fake, data):
    grist.run_sync()
    transactions = fake.doc()["tables"]["Transactions"]["rows"]
    transactions.clear()  # an incremental sync only adds rows newer than the newest there
    assert counts(grist.run_sync(), "Transactions") == (1, 0, 0)  # the table was empty
    transactions.clear()
    transactions[1] = {"App_ID": 0}  # an older row only: nothing is newer than it
    assert counts(grist.run_sync(), "Transactions") == (1, 0, 0)

    fake.doc()["tables"]["Purchase_items"]["rows"].clear()
    status = json.loads(open(grist.settings.GRIST_STATUS_FILE).read())
    status["last_full_at"] = (timezone.now() - timedelta(days=2)).isoformat()
    with open(grist.settings.GRIST_STATUS_FILE, "w") as f:
        json.dump(status, f)
    status = grist.run_sync()
    assert status["full"] is True
    assert counts(status, "Transactions") == (0, 0, 1)  # the stray row
    assert counts(status, "Purchase_items") == (1, 0, 0)


def test_a_failed_sync_is_recorded_and_the_command_fails(fake, data, capsys):
    first = grist.run_sync()
    fake.down = True
    with pytest.raises(SystemExit):
        call_command("sync_grist")
    status = grist.read_status()
    assert status["ok"] is False
    assert "not reachable" in status["error"]
    assert status["last_ok_at"] == first["finished_at"]
    assert status["doc_id"] == "doc1"  # still known for the link


def test_command_without_grist_does_nothing(settings, capsys):
    settings.GRIST_URL = ""
    call_command("sync_grist")
    assert "not set up" in capsys.readouterr().out


def test_sync_api(fake, data, admin, auth_client, make_user):
    client = auth_client(admin)
    body = client.get("/api/v1/grist/").json()
    assert (body["configured"], body["doc_url"], body["status"]) == (True, "", {})

    body = client.post("/api/v1/grist/sync/").json()
    assert body["status"]["ok"] is True
    assert body["doc_url"] == "https://grist.example/doc/doc1"
    assert client.get("/api/v1/grist/").json()["status"]["tables"]["Developers"]["added"] == 2

    # A day after the last full check, "Sync now" still leaves it to the timer.
    status = grist.read_status() | {
        "last_full_at": (timezone.now() - timedelta(days=2)).isoformat()
    }
    with open(grist.settings.GRIST_STATUS_FILE, "w") as f:
        json.dump(status, f)
    assert client.post("/api/v1/grist/sync/").json()["status"]["full"] is False

    manager = auth_client(make_user(Roles.MANAGER))
    assert manager.get("/api/v1/grist/").status_code == 403
    assert manager.post("/api/v1/grist/sync/").status_code == 403


def test_sync_api_without_grist_or_while_running(fake, admin, auth_client, settings, monkeypatch):
    client = auth_client(admin)

    def busy(**kwargs):
        raise grist.SyncBusy("busy")

    monkeypatch.setattr(grist, "run_sync", busy)
    assert client.post("/api/v1/grist/sync/").status_code == 409
    settings.GRIST_PASSWORD = ""
    assert client.post("/api/v1/grist/sync/").status_code == 400
    assert client.get("/api/v1/grist/").json()["configured"] is False
