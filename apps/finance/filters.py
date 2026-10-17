import django_filters

from .models import AccountTransaction, DeveloperAccount


class DeveloperAccountFilter(django_filters.FilterSet):
    department = django_filters.CharFilter(field_name="developer__department", lookup_expr="iexact")
    balance_min = django_filters.NumberFilter(field_name="balance", lookup_expr="gte")
    balance_max = django_filters.NumberFilter(field_name="balance", lookup_expr="lte")
    in_minus = django_filters.BooleanFilter(
        method="filter_in_minus", help_text="true: wallets below zero; false: zero or more."
    )
    building = django_filters.NumberFilter(
        field_name="developer__building", help_text="The developers' home building (an id)."
    )

    class Meta:
        model = DeveloperAccount
        fields = ["status", "developer", "department", "building"]

    def filter_in_minus(self, queryset, name, value):
        if value is None:
            return queryset
        return queryset.filter(balance__lt=0) if value else queryset.filter(balance__gte=0)


class AccountTransactionFilter(django_filters.FilterSet):
    developer = django_filters.NumberFilter(field_name="account__developer")
    building = django_filters.NumberFilter(
        field_name="account__developer__building", help_text="The developers' home building."
    )
    created_after = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_before = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="lt")

    class Meta:
        model = AccountTransaction
        fields = ["account", "developer", "kind", "method", "reference"]
