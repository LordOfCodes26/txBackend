"""Excel exports: developers with their cards, money, goods and stock.

Each export is limited like the matching lists: building managers and owners get their
buildings only. The developer sheet uses the import's columns, so it can be edited and
imported back.
"""

from datetime import date

from django.db.models import Prefetch
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.developers.models import Developer
from apps.finance.models import AccountTransaction, DeveloperAccount
from apps.goods.models import Good, InventoryMovement
from apps.purchases.models import Purchase, PurchaseItem, PurchaseStatus
from apps.rfid.models import RFIDCardAssignment
from apps.rfid.scope import building_scope
from apps.stats.services import _local_bounds

from .imports import CARD_COLUMNS, DEVELOPER_COLUMNS
from .xlsx import Column, Sheet, workbook

NAME = Column("full_name", _("Full name"), width=24)
EMPLOYEE = Column("employee_number", _("Employee no."), width=14)


def _local(moment):
    return timezone.localtime(moment).replace(tzinfo=None) if moment else None


def developers_workbook(user) -> bytes:
    developers = Developer.objects.select_related("building").order_by("employee_number")
    scope = building_scope(user, "developer.view")
    if scope is not None:
        developers = developers.filter(building__in=scope)
    cards = {
        a.developer_id: a.card
        for a in RFIDCardAssignment.objects.filter(unassigned_at__isnull=True).select_related(
            "card"
        )
    }
    rows = []
    for d in developers:
        card = cards.get(d.pk)
        rows.append(
            [
                d.employee_number,
                d.full_name,
                d.phone,
                d.home_address,
                d.birthday,
                d.department,
                d.position_title,
                d.building.code if d.building else None,
                d.start_date,
                d.out_date,
                d.status,
                card.uid if card else None,
                card.label if card else None,
            ]
        )
    return workbook([Sheet(_("Developers"), DEVELOPER_COLUMNS + CARD_COLUMNS[:2], rows)])


def money_workbook(user, first: date, last: date) -> bytes:
    start, end = _local_bounds(first, last)
    accounts = DeveloperAccount.objects.select_related("developer__building").order_by(
        "developer__employee_number"
    )
    transactions = (
        AccountTransaction.objects.filter(created_at__gte=start, created_at__lt=end)
        .select_related("account__developer")
        .order_by("created_at", "id")
    )
    purchases = (
        Purchase.objects.filter(
            status=PurchaseStatus.CONFIRMED, confirmed_at__gte=start, confirmed_at__lt=end
        )
        .select_related("seller", "service_position", "developer")
        .prefetch_related(Prefetch("items", PurchaseItem.objects.select_related("good")))
        .order_by("confirmed_at", "id")
    )
    scope = building_scope(user, "finance.view")
    if scope is not None:
        accounts = accounts.filter(developer__building__in=scope)
        transactions = transactions.filter(account__developer__building__in=scope)
        purchases = purchases.filter(service_position__building__in=scope)

    balances = Sheet(
        _("Balances"),
        [
            EMPLOYEE,
            NAME,
            Column("building", _("Building"), width=12),
            Column("status", _("Account status"), width=14),
            Column("balance", _("Balance"), width=12, kind="money"),
        ],
        [
            [
                a.developer.employee_number,
                a.developer.full_name,
                a.developer.building.code if a.developer.building else None,
                a.status,
                a.balance,
            ]
            for a in accounts
        ],
    )
    ledger = Sheet(
        _("Transactions"),
        [
            Column("time", _("Time"), width=17, kind="datetime"),
            EMPLOYEE,
            NAME,
            Column("kind", _("Kind"), width=12),
            Column("amount", _("Amount"), width=12, kind="money"),
            Column("balance_after", _("Balance after"), width=13, kind="money"),
            Column("description", _("Description"), width=30),
            Column("reference", _("Reference"), width=16),
        ],
        [
            [
                _local(t.created_at),
                t.account.developer.employee_number,
                t.account.developer.full_name,
                t.kind,
                t.amount,
                t.balance_after,
                t.description,
                t.reference,
            ]
            for t in transactions
        ],
    )
    sales = Sheet(
        _("Purchases"),
        [
            Column("time", _("Time"), width=17, kind="datetime"),
            Column("purchase", _("Purchase no."), width=12, kind="int"),
            Column("seller", _("Seller"), width=20),
            Column("counter", _("Service position"), width=18),
            EMPLOYEE,
            NAME,
            Column("items", _("Items"), width=40),
            Column("total", _("Total"), width=12, kind="money"),
        ],
        [
            [
                _local(p.confirmed_at),
                p.pk,
                p.seller.name,
                p.service_position.name,
                p.developer.employee_number if p.developer else None,
                p.developer.full_name if p.developer else None,
                ", ".join(f"{i.good.name} ×{i.quantity}" for i in p.items.all()),
                p.total,
            ]
            for p in purchases
        ],
    )
    return workbook([balances, ledger, sales])


def goods_workbook(user, first: date, last: date) -> bytes:
    start, end = _local_bounds(first, last)
    goods = Good.objects.select_related("service_position__seller").order_by(
        "service_position__seller__name", "service_position__name", "name"
    )
    movements = (
        InventoryMovement.objects.filter(created_at__gte=start, created_at__lt=end)
        .select_related("good__service_position__seller")
        .order_by("created_at", "id")
    )
    scope = building_scope(user, "good.view")
    if scope is not None:
        goods = goods.filter(service_position__building__in=scope)
        movements = movements.filter(good__service_position__building__in=scope)
    seller = Column("seller", _("Seller"), width=20)
    counter = Column("counter", _("Service position"), width=18)
    good = Column("good", _("Good"), width=24)
    catalog = Sheet(
        _("Goods"),
        [
            seller,
            counter,
            good,
            Column("kind", _("Kind"), width=10),
            Column("price", _("Price"), width=10, kind="money"),
            Column("track_stock", _("Track stock"), width=11),
            Column("quantity", _("In stock"), width=10, kind="int"),
            Column("is_active", _("For sale"), width=9),
        ],
        [
            [
                g.service_position.seller.name,
                g.service_position.name,
                g.name,
                g.kind,
                g.price,
                g.track_stock,
                g.quantity if g.track_stock else None,
                g.is_active,
            ]
            for g in goods
        ],
    )
    stock = Sheet(
        _("Stock movements"),
        [
            Column("time", _("Time"), width=17, kind="datetime"),
            seller,
            good,
            Column("kind", _("Kind"), width=14),
            Column("change", _("Change"), width=9, kind="int"),
            Column("after", _("Stock after"), width=11, kind="int"),
            Column("reason", _("Reason"), width=30),
            Column("reference", _("Reference"), width=16),
        ],
        [
            [
                _local(m.created_at),
                m.good.service_position.seller.name,
                m.good.name,
                m.kind,
                m.quantity_delta,
                m.quantity_after,
                m.reason,
                m.reference,
            ]
            for m in movements
        ],
    )
    return workbook([catalog, stock])
