import django_filters

from .models import AccountTransaction, DeveloperAccount


class DeveloperAccountFilter(django_filters.FilterSet):
    department = django_filters.CharFilter(field_name="developer__department", lookup_expr="iexact")
    balance_min = django_filters.NumberFilter(field_name="balance", lookup_expr="gte")
    balance_max = django_filters.NumberFilter(field_name="balance", lookup_expr="lte")

    class Meta:
        model = DeveloperAccount
        fields = ["status", "developer", "department"]


class AccountTransactionFilter(django_filters.FilterSet):
    developer = django_filters.NumberFilter(field_name="account__developer")
    created_after = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_before = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="lt")

    class Meta:
        model = AccountTransaction
        fields = ["account", "developer", "kind", "reference"]
