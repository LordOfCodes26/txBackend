import django_filters

from .models import Booking


class BookingFilter(django_filters.FilterSet):
    seller = django_filters.NumberFilter(field_name="good__service_position__seller")
    start_after = django_filters.IsoDateTimeFilter(field_name="start", lookup_expr="gte")
    start_before = django_filters.IsoDateTimeFilter(field_name="start", lookup_expr="lt")
    date = django_filters.DateFilter(field_name="start", lookup_expr="date")
    # INDOOR or OUTDOOR courts.
    place = django_filters.CharFilter(field_name="good__rental__place")
    # NOT_PAID (reserved) or PAID.
    state = django_filters.ChoiceFilter(
        method="filter_state",
        choices=[("NOT_PAID", "Not paid"), ("PAID", "Paid")],
    )

    class Meta:
        model = Booking
        fields = ["good", "seller", "developer", "date"]

    def filter_state(self, queryset, name, value):
        if value == "NOT_PAID":
            return queryset.filter(status="RESERVED")
        return queryset.filter(status="PAID")
