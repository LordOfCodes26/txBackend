import django_filters

from .models import User


class UserFilter(django_filters.FilterSet):
    # One role code, or several separated by commas (e.g. both seller roles).
    role = django_filters.CharFilter(method="filter_role")

    class Meta:
        model = User
        fields = ["is_active", "role"]

    def filter_role(self, queryset, name, value):
        codes = [code.strip() for code in value.split(",") if code.strip()]
        return queryset.filter(roles__code__in=codes).distinct() if codes else queryset
