import django_filters

from .models import Seller, ServicePosition


class SellerFilter(django_filters.FilterSet):
    has_user = django_filters.BooleanFilter(field_name="user", lookup_expr="isnull", exclude=True)

    class Meta:
        model = Seller
        fields = ["status", "has_user"]


class ServicePositionFilter(django_filters.FilterSet):
    class Meta:
        model = ServicePosition
        fields = ["seller", "is_active"]
