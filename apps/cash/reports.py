"""The Cash control figures, by company-local day: the money from the stores' cash ledger,
the price changes from the audit log (which records every one, whoever made it)."""

from datetime import date
from decimal import Decimal

from django.db.models import Count, Max, Q, Sum
from django.db.models.functions import TruncDate

from apps.audit.models import AuditLog
from apps.goods.models import Good, GoodKind
from apps.sellers.models import Seller
from apps.stats.services import _days, _local_bounds

from .models import CashEntry, CashKind, CountOutcome, StockCount
from .services import works_for

ZERO = Decimal("0.00")
KINDS = {
    "sales": CashKind.SALE,
    "returned": CashKind.RETURN,
    "handed_over": CashKind.HANDOVER,
    "charged": CashKind.MISSING,
}


def _sums() -> dict:
    return {name: Sum("amount", filter=Q(kind=kind)) for name, kind in KINDS.items()}


def _figures(row: dict) -> dict:
    """What came in and went out, as positive amounts. `expected` is the cash from sales:
    sales less returns. Handovers and charges cancelled later net out."""
    sales = row.get("sales") or ZERO
    returned = -(row.get("returned") or ZERO)
    return {
        "sales": sales,
        "returned": returned,
        "expected": sales - returned,
        "handed_over": -(row.get("handed_over") or ZERO),
        "charged": row.get("charged") or ZERO,
    }


def _by_seller(qs, **annotations) -> dict[int, dict]:
    return {r["seller"]: r for r in qs.order_by().values("seller").annotate(**annotations)}


def stores(sellers, first: date, last: date) -> list[dict]:
    """Per store that takes cash, or ever did: the period's figures, what it owed before and
    after it, what it owes now, its price changes and missing goods. The most owed first."""
    start, end = _local_bounds(first, last)
    entries = CashEntry.objects.filter(seller__in=sellers)
    before = _by_seller(entries.filter(created_at__lt=start), total=Sum("amount"))
    during = _by_seller(
        entries.filter(created_at__gte=start, created_at__lt=end),
        **_sums(),
        sales_count=Count("pk", filter=Q(kind=CashKind.SALE)),
        returned_count=Count("pk", filter=Q(kind=CashKind.RETURN)),
    )
    now = _by_seller(
        entries,
        total=Sum("amount"),
        last_handover_at=Max(
            "created_at",
            filter=Q(kind=CashKind.HANDOVER, reverses__isnull=True, reversal__isnull=True),
        ),
    )
    changes: dict[int, int] = {}
    for change in price_changes(sellers, first, last):
        changes[change["seller"]] = changes.get(change["seller"], 0) + 1
    counts = _by_seller(
        StockCount.objects.filter(seller__in=sellers),
        last=Max("created_at"),
        open=Count("pk", filter=Q(outcome=CountOutcome.OPEN)),
        missing=Sum("missing_value", filter=Q(outcome=CountOutcome.OPEN)),
    )
    rows = []
    shown = Q(accepts_cash=True) | Q(pk__in=list(now))
    for seller in Seller.objects.filter(pk__in=sellers.values("pk")).filter(shown):
        period = during.get(seller.pk, {})
        figures = _figures(period)
        opening = before.get(seller.pk, {}).get("total") or ZERO
        closing = opening + figures["expected"] - figures["handed_over"] + figures["charged"]
        counted = counts.get(seller.pk, {})
        rows.append(
            {
                "id": seller.pk,
                "name": seller.name,
                "accepts_cash": seller.accepts_cash,
                "opening": opening,
                **figures,
                "sales_count": period.get("sales_count", 0),
                "returned_count": period.get("returned_count", 0),
                "closing": closing,
                "owed": now.get(seller.pk, {}).get("total") or ZERO,
                "last_handover_at": now.get(seller.pk, {}).get("last_handover_at"),
                "price_changes": changes.get(seller.pk, 0),
                "open_counts": counted.get("open", 0),
                "open_missing": counted.get("missing") or ZERO,
                "last_count_at": counted.get("last"),
            }
        )
    rows.sort(key=lambda r: (-r["owed"], r["name"].lower()))
    return rows


def daily(seller: Seller, first: date, last: date) -> list[dict]:
    """One row per day (newest first): the cash from sales, what was handed over and what
    the store owed at the end of the day. Days without any change are left out."""
    start, end = _local_bounds(first, last)
    entries = CashEntry.objects.filter(seller=seller)
    running = entries.filter(created_at__lt=start).aggregate(t=Sum("amount"))["t"] or ZERO
    rows = {
        r["day"]: r
        for r in entries.filter(created_at__gte=start, created_at__lt=end)
        .annotate(day=TruncDate("created_at", tzinfo=start.tzinfo))
        .order_by()
        .values("day")
        .annotate(**_sums())
    }
    out = []
    for day in _days(first, last):
        if day not in rows:
            continue
        figures = _figures(rows[day])
        running += figures["expected"] - figures["handed_over"] + figures["charged"]
        out.append({"date": day.isoformat(), **figures, "owed": running})
    out.reverse()
    return out


def price_changes(sellers, first: date, last: date) -> list[dict]:
    """The prices changed and goods added at `sellers` in the period, newest first. Courts
    are left out (never paid in cash). Goods deleted for good are gone from the list."""
    start, end = _local_bounds(first, last)
    catalog = {
        str(g.pk): g
        for g in Good.all_objects.filter(service_position__seller__in=sellers)
        .exclude(kind=GoodKind.RENTAL)
        .select_related("service_position__seller")
    }
    logs = AuditLog.objects.filter(
        Q(action="good.created") | Q(action="good.updated", new_values__has_key="price"),
        entity_type=Good._meta.label_lower,
        entity_id__in=list(catalog),
        created_at__gte=start,
        created_at__lt=end,
    )
    works: dict[tuple, bool] = {}
    rows = []
    for entry in logs.select_related("actor").order_by("-created_at", "-id"):
        good = catalog[entry.entity_id]
        seller = good.service_position.seller
        key = (entry.actor_id, seller.pk)
        if key not in works:
            works[key] = entry.actor is not None and works_for(entry.actor, seller.pk)
        rows.append(
            {
                "id": entry.pk,
                "good": good.pk,
                "good_name": good.name,
                "seller": seller.pk,
                "seller_name": seller.name,
                "track_stock": good.track_stock,
                "old_price": Decimal(str(entry.old_values["price"]))
                if entry.action == "good.updated"
                else None,
                "new_price": Decimal(str(entry.new_values["price"])),
                "by": entry.actor_username,
                "by_store": works[key],
                "created_at": entry.created_at,
            }
        )
    return rows
