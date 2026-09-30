import django_filters

from .models import SellerPayment, SellerTransaction


class SellerTransactionFilter(django_filters.FilterSet):
    seller = django_filters.NumberFilter(field_name="account__seller")
    created_after = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_before = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="lt")

    class Meta:
        model = SellerTransaction
        fields = ["seller", "kind", "reference"]


class SellerPaymentFilter(django_filters.FilterSet):
    created_after = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_before = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="lt")

    class Meta:
        model = SellerPayment
        fields = ["seller", "status"]
