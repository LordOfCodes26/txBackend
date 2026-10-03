import django_filters

from .models import Developer


class DeveloperFilter(django_filters.FilterSet):
    department = django_filters.CharFilter(lookup_expr="iexact")
    started_after = django_filters.DateFilter(field_name="start_date", lookup_expr="gte")
    started_before = django_filters.DateFilter(field_name="start_date", lookup_expr="lte")
    out_after = django_filters.DateFilter(field_name="out_date", lookup_expr="gte")
    out_before = django_filters.DateFilter(field_name="out_date", lookup_expr="lte")
    birthday_month = django_filters.NumberFilter(
        field_name="birthday", lookup_expr="month", help_text="1-12, e.g. for a birthday list."
    )
    has_user = django_filters.BooleanFilter(field_name="user", lookup_expr="isnull", exclude=True)

    class Meta:
        model = Developer
        fields = ["status", "department", "building"]
