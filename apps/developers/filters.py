import django_filters

from .models import Developer


class DeveloperFilter(django_filters.FilterSet):
    department = django_filters.CharFilter(lookup_expr="iexact")
    started_after = django_filters.DateFilter(field_name="start_date", lookup_expr="gte")
    started_before = django_filters.DateFilter(field_name="start_date", lookup_expr="lte")
    has_user = django_filters.BooleanFilter(field_name="user", lookup_expr="isnull", exclude=True)

    class Meta:
        model = Developer
        fields = ["status", "department", "manager"]
