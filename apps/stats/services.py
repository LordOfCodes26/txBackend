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
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.attendance.models import DailyAttendance
from apps.attendance.occupancy import occupancy
from apps.developers.models import Developer, DeveloperStatus
from apps.finance.models import AccountTransaction, DeveloperAccount, TransactionKind
from apps.purchases.models import Purchase, PurchaseKind, PurchaseStatus
from apps.rfid.models import Building
from apps.rfid.scope import sellers_with_positions_in
from apps.sellers.models import Seller

ZERO = Decimal("0.00")


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _money(value) -> str:
    return f"{(value or ZERO):.2f}"


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


def money(first: date, last: date, buildings=None) -> dict:
    start, end = _local_bounds(first, last)
    accounts = DeveloperAccount.objects.all()
    period = AccountTransaction.objects.filter(created_at__gte=start, created_at__lt=end)
    sales = Purchase.objects.filter(
        status=PurchaseStatus.CONFIRMED, confirmed_at__gte=start, confirmed_at__lt=end
    )
    if buildings is not None:
        accounts = accounts.filter(developer__building__in=buildings)
        period = period.filter(account__developer__building__in=buildings)
        sales = sales.filter(service_position__building__in=buildings)
    by_status = dict(accounts.order_by().values_list("status").annotate(n=Count("pk")))
    totals = period.aggregate(
        deposits=Sum("amount", filter=Q(kind=TransactionKind.DEPOSIT)),
        deposit_count=Count("pk", filter=Q(kind=TransactionKind.DEPOSIT)),
        spending=Sum("amount", filter=Q(kind=TransactionKind.PURCHASE)),
        spending_count=Count("pk", filter=Q(kind=TransactionKind.PURCHASE)),
    )
    per_day = {
        row["day"]: row
        for row in period.annotate(day=TruncDate("created_at", tzinfo=start.tzinfo))
        .order_by()
        .values("day")
        .annotate(
            deposits=Sum("amount", filter=Q(kind=TransactionKind.DEPOSIT)),
            spending=Sum("amount", filter=Q(kind=TransactionKind.PURCHASE)),
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
            "total_balance": _money(accounts.aggregate(t=Sum("balance"))["t"]),
        },
        "deposits": {"total": _money(totals["deposits"]), "count": totals["deposit_count"]},
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
    period. Limited to `buildings`: stores with a position there, sales at those positions."""
    start, end = _local_bounds(first, last)
    sellers = Seller.objects.all()
    sales = Purchase.objects.filter(
        status=PurchaseStatus.CONFIRMED, confirmed_at__gte=start, confirmed_at__lt=end
    )
    if buildings is not None:
        sellers = sellers_with_positions_in(buildings)
        sales = sales.filter(service_position__building__in=buildings)

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
    rows = []
    for seller in sellers.order_by("name"):
        s = sold.get(seller.pk, {})
        row = {
            "id": seller.pk,
            "name": seller.name,
            "status": seller.status,
            "sales_total": _money(s.get("sales_total")),
            "sales_count": s.get("sales_count", 0),
            "bookings_total": _money(s.get("bookings_total")),
            "bookings_count": s.get("bookings_count", 0),
        }
        rows.append(row)
    rows.sort(key=lambda r: Decimal(r["sales_total"]) + Decimal(r["bookings_total"]), reverse=True)
    return rows


def developer_money(first: date, last: date, buildings=None) -> list[dict]:
    """Per developer with deposits or spending in the period: deposit total and count,
    spending total and count, and the balance now. Biggest deposit total first (then the
    most deposits). Limited to the developers of `buildings`."""
    start, end = _local_bounds(first, last)
    period = AccountTransaction.objects.filter(
        created_at__gte=start,
        created_at__lt=end,
        kind__in=(TransactionKind.DEPOSIT, TransactionKind.PURCHASE),
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
            "account__balance",
        )
        .annotate(
            deposits=Sum("amount", filter=Q(kind=TransactionKind.DEPOSIT)),
            deposit_count=Count("pk", filter=Q(kind=TransactionKind.DEPOSIT)),
            spending=Sum("amount", filter=Q(kind=TransactionKind.PURCHASE)),
            spending_count=Count("pk", filter=Q(kind=TransactionKind.PURCHASE)),
        )
    )
    out = [
        {
            "id": row["account__developer"],
            "employee_number": row["account__developer__employee_number"],
            "full_name": row["account__developer__full_name"],
            "building": row["account__developer__building__code"],
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


def finance_stats(first: date, last: date, buildings=None) -> dict:
    """The money half of `company_stats`, the per-seller comparison and the per-developer
    deposits and spending."""
    return {
        **_frame(first, last, buildings),
        "money": money(first, last, buildings),
        "sellers": seller_comparison(first, last, buildings),
        "developers": developer_money(first, last, buildings),
    }
