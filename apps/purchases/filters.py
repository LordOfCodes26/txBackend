import django_filters
from django.db.models.functions import Coalesce

from .models import Purchase


def with_moment(queryset):
    """`moment`: when a purchase counts. Paid ones (refunded too) when they were paid, the
    others (open, cancelled) when they were opened."""
    return queryset.annotate(moment=Coalesce("confirmed_at", "created_at"))


class PurchaseFilter(django_filters.FilterSet):
    confirmed_after = django_filters.IsoDateTimeFilter(field_name="confirmed_at", lookup_expr="gte")
    confirmed_before = django_filters.IsoDateTimeFilter(field_name="confirmed_at", lookup_expr="lt")
    when_after = django_filters.IsoDateTimeFilter(
        method="filter_when", help_text="When it counts (paid, else opened) at or after."
    )
    when_before = django_filters.IsoDateTimeFilter(
        method="filter_when", help_text="When it counts (paid, else opened) before."
    )
    total_min = django_filters.NumberFilter(field_name="total", lookup_expr="gte")
    total_max = django_filters.NumberFilter(field_name="total", lookup_expr="lte")

    class Meta:
        model = Purchase
        fields = ["kind", "status", "seller", "service_position", "developer"]

    def filter_when(self, queryset, name, value):
        lookup = "gte" if name == "when_after" else "lt"
        return with_moment(queryset).filter(**{f"moment__{lookup}": value})
