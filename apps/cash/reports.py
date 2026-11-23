"""What each store sold, by company-local day: by card (finance pays it to the store) and in
cash (the seller has it already). Paid sales count on the day they were paid, deleted ones not
at all, as in the statistics. Price changes come from the audit log."""

from datetime import date
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate

from apps.audit.models import AuditLog
from apps.goods.models import Good, GoodKind
from apps.purchases.models import Payment, Purchase, PurchaseStatus
from apps.sellers.models import Seller, SellerStatus
from apps.stats.services import _local_bounds

from .services import works_for

ZERO = Decimal("0.00")


def _paid(sellers, first: date, last: date):
    start, end = _local_bounds(first, last)
    return Purchase.objects.filter(
        seller__in=sellers,
        status=PurchaseStatus.CONFIRMED,
        confirmed_at__gte=start,
        confirmed_at__lt=end,
    ).order_by()


SUMS = {
    "card_total": Sum("total", filter=Q(payment=Payment.WALLET), default=ZERO),
    "card_count": Count("pk", filter=Q(payment=Payment.WALLET)),
    "cash_total": Sum("total", filter=Q(payment=Payment.CASH), default=ZERO),
    "cash_count": Count("pk", filter=Q(payment=Payment.CASH)),
}


def stores(sellers, first: date, last: date) -> list[dict]:
    """Per open store, and any other with sales in the period: its card sales (till sales and
    court bookings: what to pay it), its cash sales and its price changes. The most to pay
    first."""
    sold = {r["seller"]: r for r in _paid(sellers, first, last).values("seller").annotate(**SUMS)}
    changes: dict[int, int] = {}
    for change in price_changes(sellers, first, last):
        changes[change["seller"]] = changes.get(change["seller"], 0) + 1
    rows = []
    for seller in Seller.objects.filter(pk__in=sellers.values("pk")).filter(
        Q(status=SellerStatus.ACTIVE) | Q(pk__in=list(sold))
    ):
        figures = {k: sold.get(seller.pk, {}).get(k) or 0 for k in SUMS}
        rows.append(
            {
                "id": seller.pk,
                "name": seller.name,
                "accepts_cash": seller.accepts_cash,
                **figures,
                "total": figures["card_total"] + figures["cash_total"],
                "price_changes": changes.get(seller.pk, 0),
            }
        )
    rows.sort(key=lambda r: (-r["card_total"], -r["cash_total"], r["name"].lower()))
    return rows


def daily(seller: Seller, first: date, last: date) -> list[dict]:
    """The store's days with sales, newest first."""
    tz = _local_bounds(first, last)[0].tzinfo
    rows = (
        _paid([seller], first, last)
        .annotate(day=TruncDate("confirmed_at", tzinfo=tz))
        .values("day")
        .annotate(**SUMS)
        .order_by("-day")
    )
    return [
        {
            "date": r["day"].isoformat(),
            **{k: r[k] for k in SUMS},
            "total": r["card_total"] + r["cash_total"],
        }
        for r in rows
    ]


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
