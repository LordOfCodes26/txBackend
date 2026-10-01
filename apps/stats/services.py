"""Company statistics for the BOSS dashboard: people and attendance, money.

Read-only aggregates over a period of company-local days. Every figure comes straight from
the source tables (attendance summaries, ledgers), so it matches the detail lists.
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
from apps.seller_finance.models import (
    PayoutStatus,
    SellerAccount,
    SellerPayment,
    SellerTransaction,
    SellerTransactionKind,
)

ZERO = Decimal("0.00")
PENDING_PAYOUTS = (PayoutStatus.REQUESTED, PayoutStatus.APPROVED, PayoutStatus.PROCESSING)


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _money(value) -> str:
    return f"{(value or ZERO):.2f}"


def _local_bounds(first: date, last: date):
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(first, time.min), tz)
    end = timezone.make_aware(datetime.combine(last + timedelta(days=1), time.min), tz)
    return start, end


def people(first: date, last: date) -> dict:
    alive = Developer.objects.filter(deleted_at__isnull=True)
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
        for row in DailyAttendance.objects.filter(work_date__range=(first, last))
        .order_by()
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
        "inside_now": occupancy(),
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


def money(first: date, last: date) -> dict:
    start, end = _local_bounds(first, last)
    accounts = DeveloperAccount.objects.all()
    by_status = dict(accounts.order_by().values_list("status").annotate(n=Count("pk")))
    period = AccountTransaction.objects.filter(created_at__gte=start, created_at__lt=end)
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

    seller_period = SellerTransaction.objects.filter(created_at__gte=start, created_at__lt=end)
    seller_totals = seller_period.aggregate(
        earnings=Sum("amount", filter=Q(kind=SellerTransactionKind.SALE)),
        payouts=Sum("amount", filter=Q(kind=SellerTransactionKind.PAYOUT)),
    )
    pending = SellerPayment.objects.filter(status__in=PENDING_PAYOUTS).aggregate(
        count=Count("pk"), amount=Sum("amount")
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
        "sellers": {
            "total_balance": _money(SellerAccount.objects.aggregate(t=Sum("balance"))["t"]),
            "earnings": _money(seller_totals["earnings"]),
            "payouts_paid": _money(-(seller_totals["payouts"] or ZERO)),
            "payouts_pending": {
                "count": pending["count"],
                "amount": _money(pending["amount"]),
            },
        },
    }


def company_stats(first: date, last: date) -> dict:
    return {
        "period": {
            "date_from": first.isoformat(),
            "date_to": last.isoformat(),
            "days": (last - first).days + 1,
        },
        "people": people(first, last),
        "money": money(first, last),
    }
