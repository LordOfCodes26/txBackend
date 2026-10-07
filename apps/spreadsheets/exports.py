"""Excel exports: developers with their cards, money, goods and stock, finance statistics.

Each export is limited like the matching lists: building managers and owners get their
buildings only. The developer sheet uses the import's columns, so it can be edited and
imported back.
"""

from datetime import date
from decimal import Decimal

from django.db.models import Prefetch, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.developers.filters import DeveloperFilter
from apps.developers.models import Developer
from apps.finance.models import AccountTransaction, DeveloperAccount
from apps.goods.models import Good, InventoryMovement
from apps.purchases.models import Purchase, PurchaseItem, PurchaseStatus
from apps.rfid.models import RFIDCardAssignment
from apps.rfid.scope import building_scope
from apps.stats.services import _local_bounds, finance_stats

from .imports import CARD_COLUMNS, DEVELOPER_COLUMNS
from .xlsx import Column, Sheet, workbook

NAME = Column("full_name", _("Full name"), width=24)
EMPLOYEE = Column("employee_number", _("Employee no."), width=14)


def _local(moment):
    return timezone.localtime(moment).replace(tzinfo=None) if moment else None


def developers_workbook(user, filters=None) -> bytes:
    """`filters`: the developer list's query (status, department, building, birthday_month,
    out_after, out_before, search, ...), so the file matches what the list shows."""
    developers = Developer.objects.select_related("building").order_by("employee_number")
    if filters:
        developers = DeveloperFilter(filters, queryset=developers).qs
        for term in (filters.get("search") or "").split():
            developers = developers.filter(
                Q(full_name__icontains=term)
                | Q(employee_number__icontains=term)
                | Q(department__icontains=term)
                | Q(position_title__icontains=term)
                | Q(phone__icontains=term)
            )
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
            Column("counter", _("Counter"), width=18),
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
    counter = Column("counter", _("Counter"), width=18)
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


def finance_stats_workbook(user, first: date, last: date) -> bytes:
    """The finance statistics page as a workbook: the totals, each day, each seller and
    each developer (by deposit total), with the same building limits as the page."""
    stats = finance_stats(first, last, building_scope(user, "finance.view"))
    money = stats["money"]
    label, value = Column("label", _("Figure"), width=28), Column("value", _("Value"), width=18)
    summary = [
        [str(_("From")), first.isoformat()],
        [str(_("To")), last.isoformat()],
        [str(_("Deposits")), Decimal(money["deposits"]["total"])],
        [str(_("Deposit count")), money["deposits"]["count"]],
        [str(_("Spending")), Decimal(money["spending"]["total"])],
        [str(_("Payment count")), money["spending"]["count"]],
        [str(_("Money held now")), Decimal(money["developer_accounts"]["total_balance"])],
        [str(_("Developer accounts")), money["developer_accounts"]["count"]],
    ]
    daily = [
        [date.fromisoformat(d["date"]), d.get("deposits"), d.get("spending")]
        for d in money.get("daily", [])
    ]
    sellers = [
        [
            s["name"],
            s["sales_total"],
            s["sales_count"],
            s["bookings_total"],
            s["bookings_count"],
            Decimal(s["sales_total"]) + Decimal(s["bookings_total"]),
        ]
        for s in stats["sellers"]
    ]
    developers = [
        [
            rank,
            d["employee_number"],
            d["full_name"],
            d["building"],
            d["deposits"],
            d["deposit_count"],
            d["spending"],
            d["spending_count"],
            d["balance"],
        ]
        for rank, d in enumerate(stats["developers"], start=1)
    ]
    count = lambda key, title: Column(key, title, width=12, kind="int")  # noqa: E731
    cash = lambda key, title: Column(key, title, width=14, kind="money")  # noqa: E731
    return workbook(
        [
            Sheet(_("Summary"), [label, value], summary),
            Sheet(
                _("Daily"),
                [
                    Column("date", _("Date"), width=12, kind="date"),
                    cash("deposits", _("Deposits")),
                    cash("spending", _("Spending")),
                ],
                daily,
            ),
            Sheet(
                _("Sellers"),
                [
                    Column("seller", _("Seller"), width=24),
                    cash("sales", _("Till sales")),
                    count("sales_count", _("Sales count")),
                    cash("bookings", _("Court bookings")),
                    count("bookings_count", _("Booking count")),
                    cash("total", _("Total")),
                ],
                sellers,
            ),
            Sheet(
                _("Developers"),
                [
                    Column("rank", "#", width=6, kind="int"),
                    EMPLOYEE,
                    NAME,
                    Column("building", _("Building"), width=14),
                    cash("deposits", _("Deposits")),
                    count("deposit_count", _("Deposit count")),
                    cash("spending", _("Spending")),
                    count("spending_count", _("Payment count")),
                    cash("balance", _("Balance now")),
                ],
                developers,
            ),
        ]
    )


# The purchases page's short names.
PURCHASE_STATUS = {"CONFIRMED": _("Paid"), "DRAFT": _("Draft"), "CANCELLED": _("Cancelled")}
PURCHASE_KIND = {"SALE": _("Sale"), "BOOKING": _("Booking")}


def purchases_workbook(purchases, first: date, last: date) -> bytes:
    """The purchases page as a workbook: one row per purchase and one per item. Paid
    purchases count on the day they were paid, drafts and cancelled ones on the day they
    were started (as on the page). `purchases`: already narrowed to what the user may see."""
    start, end = _local_bounds(first, last)
    purchases = (
        purchases.filter(
            Q(status=PurchaseStatus.CONFIRMED, confirmed_at__gte=start, confirmed_at__lt=end)
            | (~Q(status=PurchaseStatus.CONFIRMED) & Q(created_at__gte=start, created_at__lt=end))
        )
        .select_related("seller", "service_position", "developer", "reader")
        .prefetch_related("items__good")
        .order_by("created_at", "id")
    )
    number = Column("purchase", _("Purchase no."), width=12, kind="int")
    when = Column("time", _("Time"), width=17, kind="datetime")
    state = Column("status", _("Status"), width=11)
    seller = Column("seller", _("Seller"), width=20)
    counter = Column("counter", _("Counter"), width=18)
    buyer_no = Column("employee_number", _("Buyer no."), width=14)
    buyer = Column("buyer", _("Buyer"), width=22)
    heads, lines = [], []
    for p in purchases:
        time = _local(p.confirmed_at if p.status == PurchaseStatus.CONFIRMED else p.created_at)
        who = [p.developer.employee_number, p.developer.full_name] if p.developer else [None, None]
        items = list(p.items.all())
        heads.append(
            [
                p.pk,
                time,
                str(PURCHASE_STATUS.get(p.status, p.status)),
                str(PURCHASE_KIND.get(p.kind, p.kind)),
                p.seller.name,
                p.service_position.name,
                *who,
                p.reader.code if p.reader else None,
                sum(i.quantity for i in items),
                p.total,
            ]
        )
        for i in items:
            lines.append(
                [
                    p.pk,
                    time,
                    str(PURCHASE_STATUS.get(p.status, p.status)),
                    p.seller.name,
                    p.service_position.name,
                    *who,
                    i.good.name,
                    i.quantity,
                    i.unit_price if i.unit_price is not None else i.good.price,
                    i.line_total if i.line_total is not None else (i.good.price or 0) * i.quantity,
                    _local(i.start),
                ]
            )
    money = lambda key, title: Column(key, title, width=12, kind="money")  # noqa: E731
    return workbook(
        [
            Sheet(
                _("Purchases"),
                [
                    number,
                    when,
                    state,
                    Column("kind", _("Kind"), width=10),
                    seller,
                    counter,
                    buyer_no,
                    buyer,
                    Column("reader", _("Reader"), width=12),
                    Column("quantity", _("Items"), width=8, kind="int"),
                    money("total", _("Total")),
                ],
                heads,
            ),
            Sheet(
                _("Items"),
                [
                    number,
                    when,
                    state,
                    seller,
                    counter,
                    buyer_no,
                    buyer,
                    Column("good", _("Good"), width=24),
                    Column("quantity", _("Quantity"), width=9, kind="int"),
                    money("unit_price", _("Unit price")),
                    money("line_total", _("Line total")),
                    Column("start", _("Booked from"), width=17, kind="datetime"),
                ],
                lines,
            ),
        ]
    )


CARD_STATUS = {"ACTIVE": _("Active"), "BLOCKED": _("Blocked"), "RETIRED": _("Retired")}
READER_KIND = {"ATTENDANCE": _("Door"), "TILL": _("Till reader"), "ENROLL": _("Card assign reader")}


def cards_workbook(cards) -> bytes:
    """The cards list as a workbook. `cards`: already filtered and narrowed like the list."""
    rows = []
    for card in cards.order_by("uid"):
        holder = card.current_assignment
        rows.append(
            [
                card.uid,
                card.label,
                str(CARD_STATUS.get(card.status, card.status)),
                card.status_reason,
                holder.developer.employee_number if holder else None,
                holder.developer.full_name if holder else None,
                _local(holder.assigned_at) if holder else None,
                _local(card.updated_at),
            ]
        )
    return workbook(
        [
            Sheet(
                _("Cards"),
                [
                    Column("uid", _("Card UID"), width=16),
                    Column("label", _("Label"), width=16),
                    Column("status", _("Status"), width=10),
                    Column("reason", _("Reason"), width=24),
                    EMPLOYEE,
                    Column("holder", _("Holder"), width=24),
                    Column("assigned_at", _("Assigned at"), width=17, kind="datetime"),
                    Column("updated_at", _("Updated"), width=17, kind="datetime"),
                ],
                rows,
            )
        ]
    )


def readers_workbook(devices) -> bytes:
    """The readers list as a workbook. `devices`: already filtered and narrowed."""
    rows = [
        [
            d.code,
            d.name,
            str(READER_KIND.get(d.purpose, d.purpose)),
            d.building.name if d.building else None,
            d.location,
            d.seller.name if d.seller else None,
            d.allowed_ip,
            str(_("Yes") if d.is_active else _("No")),
            str(_("Online") if d.is_online else _("Offline")),
            _local(d.last_seen_at),
        ]
        for d in devices.order_by("code")
    ]
    return workbook(
        [
            Sheet(
                _("Readers"),
                [
                    Column("code", _("Code"), width=12),
                    Column("name", _("Name"), width=20),
                    Column("kind", _("Kind"), width=18),
                    Column("building", _("Building"), width=14),
                    Column("location", _("Place"), width=18),
                    Column("seller", _("Seller"), width=18),
                    Column("ip", _("Door IP"), width=15),
                    Column("active", _("Active"), width=8),
                    Column("online", _("Online"), width=9),
                    Column("last_seen", _("Last seen"), width=17, kind="datetime"),
                ],
                rows,
            )
        ]
    )
