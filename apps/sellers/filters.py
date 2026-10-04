import django_filters

from .models import Seller, SellerStatus, ServicePosition


class SellerFilter(django_filters.FilterSet):
    has_user = django_filters.BooleanFilter(field_name="user", lookup_expr="isnull", exclude=True)

    class Meta:
        model = Seller
        fields = ["status", "has_user"]


class ServicePositionFilter(django_filters.FilterSet):
    seller_status = django_filters.ChoiceFilter(
        field_name="seller__status", choices=SellerStatus.choices
    )

    class Meta:
        model = ServicePosition
        fields = ["seller", "building", "manager", "is_active", "seller_status"]
