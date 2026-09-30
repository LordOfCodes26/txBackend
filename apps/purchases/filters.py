import django_filters

from .models import Purchase


class PurchaseFilter(django_filters.FilterSet):
    confirmed_after = django_filters.IsoDateTimeFilter(field_name="confirmed_at", lookup_expr="gte")
    confirmed_before = django_filters.IsoDateTimeFilter(field_name="confirmed_at", lookup_expr="lt")
    total_min = django_filters.NumberFilter(field_name="total", lookup_expr="gte")
    total_max = django_filters.NumberFilter(field_name="total", lookup_expr="lte")

    class Meta:
        model = Purchase
        fields = ["status", "seller", "service_position", "developer"]
