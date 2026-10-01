import django_filters

from .models import Booking


class BookingFilter(django_filters.FilterSet):
    seller = django_filters.NumberFilter(field_name="good__service_position__seller")
    start_after = django_filters.IsoDateTimeFilter(field_name="start", lookup_expr="gte")
    start_before = django_filters.IsoDateTimeFilter(field_name="start", lookup_expr="lt")
    date = django_filters.DateFilter(field_name="start", lookup_expr="date")

    class Meta:
        model = Booking
        fields = ["good", "seller", "developer", "date"]
