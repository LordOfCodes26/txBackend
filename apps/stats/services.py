"""Company statistics for the BOSS dashboard: people and attendance, money.

Read-only aggregates over a period of company-local days. Every figure comes straight from
the source tables (attendance summaries, ledgers), so it matches the detail lists.

`buildings` (a building owner's view) narrows everything to those buildings: developers by
home building, their attendance and money, and the stores there (sales at positions in the
buildings; seller-wide money only for stores whose positions are all in them).
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import Avg, Count, Q, Sum
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from apps.attendance.models import DailyAttendance
from apps.attendance.occupancy import occupancy
from apps.developers.models import Developer, DeveloperStatus
from apps.finance.models import AccountTransaction, DeveloperAccount, TransactionKind
from apps.purchases.models import Purchase, PurchaseKind, PurchaseStatus
from apps.rfid.models import Building
from apps.sellers.models import Seller

ZERO = Decimal("0.00")
# Money spent: purchases less what was refunded (paid purchases changed or deleted).
SPENDING = Q(kind__in=(TransactionKind.PURCHASE, TransactionKind.REFUND))


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _money(value) -> str:
    return f"{(value or ZERO):.2f}"


DEPOSIT_TYPES = ("ECOMMERCE", "CASH", "NONE")


def _deposit_q(deposit_type: str | None = None) -> Q:
    """Deposits, of one type if given: ECOMMERCE, CASH, or NONE (made before the type was
    asked)."""
    q = Q(kind=TransactionKind.DEPOSIT)
    if deposit_type:
        q &= Q(method="" if deposit_type == "NONE" else deposit_type)
    return q


def _deposit_count(q: Q):
    """How many deposits: a reversal (a negative deposit, for one entered by mistake)
    cancels one. Their amounts net out in Sum("amount") already."""
    return Count("pk", filter=q & Q(amount__gt=0)) - Count("pk", filter=q & Q(amount__lt=0))


# When a movement counts: its time, except a deposit reversal or a correcting deposit,
# which count on the wrong deposit's time (`counts_on`).
COUNTED_AT = Coalesce("counts_on", "created_at")


def _counted_between(start, end) -> Q:
    return Q(counts_on__isnull=True, created_at__gte=start, created_at__lt=end) | Q(
        counts_on__gte=start, counts_on__lt=end
    )


def _local_bounds(first: date, last: date):
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(first, time.min), tz)
    end = timezone.make_aware(datetime.combine(last + timedelta(days=1), time.min), tz)
    return start, end


def people(first: date, last: date, buildings=None) -> dict:
    alive = Developer.objects.filter(deleted_at__isnull=True)
    attendance = DailyAttendance.objects.filter(work_date__range=(first, last))
    if buildings is not None:
        alive = alive.filter(building__in=buildings)
        attendance = attendance.filter(developer__building__in=buildings)
    by_status = dict(alive.order_by().values_list("status").annotate(n=Count("pk")))
    by_building = [
        {"building": row["building"], "name": row["building__name"], "count": row["n"]}
        for row in alive.exclude(status=DeveloperStatus.TERMINATED)
        .order_by("building__name")
        .values("building", "building__name")
        .annotate(n=Count("pk"))
    ]

    rows = {
        row["work_date"]: row
        for row in attendance.order_by()
        .values("work_date")
        .annotate(present=Count("developer", distinct=True), avg_seconds=Avg("worked_seconds"))
    }
    daily = [
        {
            "date": day.isoformat(),
            "present": rows[day]["present"] if day in rows else 0,
            "avg_worked_hours": round((rows[day]["avg_seconds"] or 0) / 3600, 2)
            if day in rows
            else 0,
        }
        for day in _days(first, last)
    ]
    worked_days = [d for d in daily if d["present"]]
    return {
        "developers": {
            "total": sum(n for s, n in by_status.items() if s != DeveloperStatus.TERMINATED),
            "by_status": {s: by_status.get(s, 0) for s in DeveloperStatus.values},
            "by_building": by_building,
        },
        "inside_now": occupancy(buildings),
        "attendance": {
            "daily": daily,
            "days_with_attendance": len(worked_days),
            "avg_present_per_day": round(
                sum(d["present"] for d in worked_days) / len(worked_days), 1
            )
            if worked_days
            else 0,
            "avg_worked_hours": round(
                sum(d["avg_worked_hours"] for d in worked_days) / len(worked_days), 2
            )
            if worked_days
            else 0,
        },
    }


def money(
    first: date, last: date, buildings=None, sales_by: str = "counter", deposit_type=None
) -> dict:
    """`sales_by`: limit store sales to `buildings` by the counter's building ("counter",
    the company dashboard) or by the buyer's home building ("buyer", finance statistics)."""
    deposit_q = _deposit_q(deposit_type)
    start, end = _local_bounds(first, last)
    accounts = DeveloperAccount.objects.all()
    period = AccountTransaction.objects.filter(_counted_between(start, end))
    sales = Purchase.objects.filter(
        status=PurchaseStatus.CONFIRMED, confirmed_at__gte=start, confirmed_at__lt=end
    )
    if buildings is not None:
        accounts = accounts.filter(developer__building__in=buildings)
        period = period.filter(account__developer__building__in=buildings)
        if sales_by == "buyer":
            sales = sales.filter(developer__building__in=buildings)
        else:
            sales = sales.filter(service_position__building__in=buildings)
    by_status = dict(accounts.order_by().values_list("status").annotate(n=Count("pk")))
    totals = period.aggregate(
        deposits=Sum("amount", filter=deposit_q),
        deposit_count=_deposit_count(deposit_q),
        spending=Sum("amount", filter=SPENDING),
        spending_count=Count("pk", filter=Q(kind=TransactionKind.PURCHASE)),
    )
    per_day = {
        row["day"]: row
        for row in period.annotate(day=TruncDate(COUNTED_AT, tzinfo=start.tzinfo))
        .order_by()
        .values("day")
        .annotate(
            deposits=Sum("amount", filter=deposit_q),
            spending=Sum("amount", filter=SPENDING),
        )
    }
    daily = [
        {
            "date": day.isoformat(),
            "deposits": _money(per_day.get(day, {}).get("deposits")),
            "spending": _money(-(per_day.get(day, {}).get("spending") or ZERO)),
        }
        for day in _days(first, last)
    ]

    store_sales = sales.aggregate(
        sales_total=Sum("total", filter=Q(kind=PurchaseKind.SALE)),
        sales_count=Count("pk", filter=Q(kind=PurchaseKind.SALE)),
        bookings_total=Sum("total", filter=Q(kind=PurchaseKind.BOOKING)),
        bookings_count=Count("pk", filter=Q(kind=PurchaseKind.BOOKING)),
    )
    return {
        "developer_accounts": {
            "count": sum(by_status.values()),
            "by_status": by_status,
            # Money held: what the wallets hold, positive balances only; what wallets below
            # zero owe is "minus" (below).
            "total_balance": _money(
                accounts.filter(balance__gt=0).aggregate(t=Sum("balance"))["t"]
            ),
        },
        # Wallets below zero now (purchases may go down to the minus limit): what they owe.
        "minus": {
            "total": _money(accounts.filter(balance__lt=0).aggregate(t=Sum("balance"))["t"]),
            "count": accounts.filter(balance__lt=0).count(),
        },
        "deposits": {
            "total": _money(totals["deposits"]),
            "count": totals["deposit_count"],
            # Every type, whatever the filter: E-commerce, Cash, and none (older deposits).
            "by_type": {
                kind: {"total": _money(row["t"]), "count": row["n"]}
                for kind in DEPOSIT_TYPES
                for row in [
                    period.aggregate(
                        t=Sum("amount", filter=_deposit_q(kind)), n=_deposit_count(_deposit_q(kind))
                    )
                ]
            },
        },
        "spending": {
            "total": _money(-(totals["spending"] or ZERO)),
            "count": totals["spending_count"],
        },
        "daily": daily,
        "store_sales": {
            "sales_total": _money(store_sales["sales_total"]),
            "sales_count": store_sales["sales_count"],
            "bookings_total": _money(store_sales["bookings_total"]),
            "bookings_count": store_sales["bookings_count"],
        },
    }


def _frame(first: date, last: date, buildings) -> dict:
    return {
        "period": {
            "date_from": first.isoformat(),
            "date_to": last.isoformat(),
            "days": (last - first).days + 1,
        },
        # null: the whole company; else the buildings these figures are limited to.
        "buildings": None
        if buildings is None
        else list(
            Building.objects.filter(pk__in=buildings).order_by("code").values("id", "code", "name")
        ),
    }


def company_stats(first: date, last: date, buildings=None) -> dict:
    return {
        **_frame(first, last, buildings),
        "people": people(first, last, buildings),
        "money": money(first, last, buildings),
    }


def seller_comparison(first: date, last: date, buildings=None) -> list[dict]:
    """Per seller (store), for the finance dashboard: store sales and bookings in the
    period. Limited to `buildings` by the BUYER: what developers of those buildings bought,
    at any store (the stores they bought from)."""
    start, end = _local_bounds(first, last)
    sellers = Seller.objects.all()
    sales = Purchase.objects.filter(
        status=PurchaseStatus.CONFIRMED, confirmed_at__gte=start, confirmed_at__lt=end
    )
    if buildings is not None:
        sales = sales.filter(developer__building__in=buildings)
        sellers = sellers.filter(pk__in=sales.values("seller"))

    sold = {
        row["seller"]: row
        for row in sales.order_by()
        .values("seller")
        .annotate(
            sales_total=Sum("total", filter=Q(kind=PurchaseKind.SALE)),
            sales_count=Count("pk", filter=Q(kind=PurchaseKind.SALE)),
            bookings_total=Sum("total", filter=Q(kind=PurchaseKind.BOOKING)),
            bookings_count=Count("pk", filter=Q(kind=PurchaseKind.BOOKING)),
        )
    }
    # Each seller's sales and bookings split by the buyer's home building (`building` null:
    # buyers without one).
    split = {}
    for row in (
        sales.order_by()
        .values("seller", "developer__building")
        .annotate(total=Sum("total"), count=Count("pk"))
    ):
        split.setdefault(row["seller"], []).append(row)
    names = dict(Building.objects.values_list("pk", "code"))
    titles = dict(Building.objects.values_list("pk", "name"))
    rows = []
    for seller in sellers.order_by("name"):
        s = sold.get(seller.pk, {})
        parts = sorted(
            split.get(seller.pk, []),
            key=lambda r: (
                r["developer__building"] is None,
                names.get(r["developer__building"], ""),
            ),
        )
        row = {
            "id": seller.pk,
            "name": seller.name,
            "status": seller.status,
            "sales_total": _money(s.get("sales_total")),
            "sales_count": s.get("sales_count", 0),
            "bookings_total": _money(s.get("bookings_total")),
            "bookings_count": s.get("bookings_count", 0),
            "by_building": [
                {
                    "building": part["developer__building"],
                    "code": names.get(part["developer__building"], ""),
                    "name": titles.get(part["developer__building"], ""),
                    "total": _money(part["total"]),
                    "count": part["count"],
                }
                for part in parts
            ],
        }
        rows.append(row)
    rows.sort(key=lambda r: Decimal(r["sales_total"]) + Decimal(r["bookings_total"]), reverse=True)
    return rows


def developer_money(first: date, last: date, buildings=None, deposit_type=None) -> list[dict]:
    """Per developer with deposits or spending in the period: deposit total and count,
    spending total and count, and the balance now. Biggest deposit total first (then the
    most deposits). Limited to the developers of `buildings`."""
    deposit_q = _deposit_q(deposit_type)
    start, end = _local_bounds(first, last)
    period = AccountTransaction.objects.filter(
        _counted_between(start, end),
        kind__in=(TransactionKind.DEPOSIT, TransactionKind.PURCHASE, TransactionKind.REFUND),
    )
    if buildings is not None:
        period = period.filter(account__developer__building__in=buildings)
    rows = (
        period.order_by()
        .values(
            "account__developer",
            "account__developer__employee_number",
            "account__developer__full_name",
            "account__developer__building__code",
            "account__developer__building__name",
            "account__balance",
        )
        .annotate(
            deposits=Sum("amount", filter=deposit_q),
            deposit_count=_deposit_count(deposit_q),
            spending=Sum("amount", filter=SPENDING),
            spending_count=Count("pk", filter=Q(kind=TransactionKind.PURCHASE)),
        )
    )
    out = [
        {
            "id": row["account__developer"],
            "employee_number": row["account__developer__employee_number"],
            "full_name": row["account__developer__full_name"],
            "building": row["account__developer__building__code"],
            "building_name": row["account__developer__building__name"],
            "deposits": _money(row["deposits"]),
            "deposit_count": row["deposit_count"],
            "spending": _money(-(row["spending"] or ZERO)),
            "spending_count": row["spending_count"],
            "balance": _money(row["account__balance"]),
        }
        for row in rows
    ]
    out.sort(key=lambda r: (-Decimal(r["deposits"]), -r["deposit_count"], r["employee_number"]))
    return out


def building_money(first: date, last: date, buildings=None, deposit_type=None) -> list[dict]:
    """Per building (the developers' home building): deposits, spending and balances now,
    and `daily` deposits and spending for the chart. Developers without a building are a
    row of their own (`id` null) when the figures are company-wide."""
    deposit_q = _deposit_q(deposit_type)
    start, end = _local_bounds(first, last)
    period = AccountTransaction.objects.filter(_counted_between(start, end))
    accounts = DeveloperAccount.objects.all()
    places = Building.objects.order_by("code")
    if buildings is not None:
        places = places.filter(pk__in=buildings)
        period = period.filter(account__developer__building__in=buildings)
        accounts = accounts.filter(developer__building__in=buildings)
    moves = {
        row["account__developer__building"]: row
        for row in period.order_by()
        .values("account__developer__building")
        .annotate(
            deposits=Sum("amount", filter=deposit_q),
            deposit_count=_deposit_count(deposit_q),
            spending=Sum("amount", filter=SPENDING),
            spending_count=Count("pk", filter=Q(kind=TransactionKind.PURCHASE)),
        )
    }
    per_day = {}
    for row in (
        period.annotate(day=TruncDate(COUNTED_AT, tzinfo=start.tzinfo))
        .order_by()
        .values("account__developer__building", "day")
        .annotate(
            deposits=Sum("amount", filter=deposit_q),
            spending=Sum("amount", filter=SPENDING),
        )
    ):
        per_day[(row["account__developer__building"], row["day"])] = row
    days = list(_days(first, last))
    held = {
        row["developer__building"]: row
        for row in accounts.order_by()
        .values("developer__building")
        .annotate(
            # Money held: positive balances only (what wallets below zero owe is "minus").
            # Not named "balance": the filters mean each wallet's balance, not this sum.
            held_total=Sum("balance", filter=Q(balance__gt=0)),
            wallets=Count("pk"),
            minus=Sum("balance", filter=Q(balance__lt=0)),
            minus_count=Count("pk", filter=Q(balance__lt=0)),
        )
    }
    rows = [(b.pk, b.code, b.name) for b in places]
    if buildings is None and (None in moves or None in held):
        rows.append((None, "", ""))  # developers without a home building
    out = []
    for pk, code, name in rows:
        m, h = moves.get(pk, {}), held.get(pk, {})
        deposits = m.get("deposits") or ZERO
        spending = -(m.get("spending") or ZERO)
        out.append(
            {
                "id": pk,
                "code": code,
                "name": name,
                "deposits": _money(deposits),
                "deposit_count": m.get("deposit_count", 0),
                "spending": _money(spending),
                "spending_count": m.get("spending_count", 0),
                "net": _money(deposits - spending),
                "balance": _money(h.get("held_total")),
                "wallets": h.get("wallets", 0),
                "minus": _money(h.get("minus")),
                "minus_count": h.get("minus_count", 0),
                "daily": [
                    {
                        "date": day.isoformat(),
                        "deposits": _money(per_day.get((pk, day), {}).get("deposits")),
                        "spending": _money(-(per_day.get((pk, day), {}).get("spending") or ZERO)),
                    }
                    for day in days
                ],
            }
        )
    return out


def finance_stats(first: date, last: date, buildings=None, deposit_type=None) -> dict:
    """The money half of `company_stats`, the per-seller comparison and the per-developer
    deposits and spending."""
    return {
        **_frame(first, last, buildings),
        # Store sales by the buyer's building, like the rest of this page.
        "money": money(first, last, buildings, sales_by="buyer", deposit_type=deposit_type),
        "sellers": seller_comparison(first, last, buildings),
        "developers": developer_money(first, last, buildings, deposit_type),
        # Per building, unless the figures are already one building's.
        "by_building": []
        if buildings is not None and len(buildings) <= 1
        else building_money(first, last, buildings, deposit_type),
    }
