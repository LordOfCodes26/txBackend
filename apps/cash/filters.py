import django_filters

from .models import CashEntry, StockCount


class CashEntryFilter(django_filters.FilterSet):
    created_after = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_before = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="lt")

    class Meta:
        model = CashEntry
        fields = ["seller", "kind"]


class StockCountFilter(django_filters.FilterSet):
    class Meta:
        model = StockCount
        fields = ["seller", "outcome"]
