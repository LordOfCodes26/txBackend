import django_filters

from .models import Good, GoodKind, InventoryMovement


class GoodFilter(django_filters.FilterSet):
    seller = django_filters.NumberFilter(field_name="service_position__seller")
    in_stock = django_filters.BooleanFilter(method="filter_in_stock")
    price_min = django_filters.NumberFilter(field_name="price", lookup_expr="gte")
    price_max = django_filters.NumberFilter(field_name="price", lookup_expr="lte")
    # Courts are listed under Playground, not with the goods.
    rental = django_filters.BooleanFilter(method="filter_rental")

    class Meta:
        model = Good
        fields = ["seller", "service_position", "kind", "is_active", "track_stock", "in_stock"]

    def filter_in_stock(self, queryset, name, value):
        # Untracked goods are always available.
        in_stock = queryset.filter(track_stock=False) | queryset.filter(quantity__gt=0)
        return in_stock if value else queryset.filter(track_stock=True, quantity=0)

    def filter_rental(self, queryset, name, value):
        rentals = {"kind": GoodKind.RENTAL}
        return queryset.filter(**rentals) if value else queryset.exclude(**rentals)


class InventoryMovementFilter(django_filters.FilterSet):
    seller = django_filters.NumberFilter(field_name="good__service_position__seller")
    created_after = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_before = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="lt")

    class Meta:
        model = InventoryMovement
        fields = ["good", "seller", "kind"]
