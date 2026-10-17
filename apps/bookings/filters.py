import django_filters

from .models import Booking


class BookingFilter(django_filters.FilterSet):
    seller = django_filters.NumberFilter(field_name="good__service_position__seller")
    start_after = django_filters.IsoDateTimeFilter(field_name="start", lookup_expr="gte")
    start_before = django_filters.IsoDateTimeFilter(field_name="start", lookup_expr="lt")
    date = django_filters.DateFilter(field_name="start", lookup_expr="date")
    # INDOOR or OUTDOOR courts.
    place = django_filters.CharFilter(field_name="good__rental__place")
    # NOT_PAID, PAID (not checked in yet) or CHECKED_IN.
    state = django_filters.ChoiceFilter(
        method="filter_state",
        choices=[("NOT_PAID", "Not paid"), ("PAID", "Paid"), ("CHECKED_IN", "Checked in")],
    )

    class Meta:
        model = Booking
        fields = ["good", "seller", "developer", "date"]

    def filter_state(self, queryset, name, value):
        if value == "NOT_PAID":
            return queryset.filter(status="RESERVED")
        if value == "PAID":
            return queryset.filter(status="PAID", checked_in_at__isnull=True)
        return queryset.filter(checked_in_at__isnull=False)
