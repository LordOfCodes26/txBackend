import django_filters

from .models import RFIDCard, RFIDCardAssignment, RFIDDevice, RFIDEvent, normalize_uid


class RFIDCardFilter(django_filters.FilterSet):
    assigned = django_filters.BooleanFilter(method="filter_assigned")
    developer = django_filters.NumberFilter(method="filter_developer")

    class Meta:
        model = RFIDCard
        fields = ["status", "assigned", "developer"]

    def filter_assigned(self, queryset, name, value):
        # `assignments__isnull=False` is needed: a LEFT JOIN on a card with no assignments
        # yields unassigned_at = NULL too.
        active = queryset.filter(assignments__isnull=False, assignments__unassigned_at__isnull=True)
        return active if value else queryset.exclude(pk__in=active.values("pk"))

    def filter_developer(self, queryset, name, value):
        return queryset.filter(
            assignments__developer_id=value, assignments__unassigned_at__isnull=True
        )


class RFIDCardAssignmentFilter(django_filters.FilterSet):
    active = django_filters.BooleanFilter(field_name="unassigned_at", lookup_expr="isnull")

    class Meta:
        model = RFIDCardAssignment
        fields = ["card", "developer", "active", "end_reason"]


class RFIDEventFilter(django_filters.FilterSet):
    uid = django_filters.CharFilter(method="filter_uid")
    event_after = django_filters.IsoDateTimeFilter(field_name="event_time", lookup_expr="gte")
    event_before = django_filters.IsoDateTimeFilter(field_name="event_time", lookup_expr="lt")

    class Meta:
        model = RFIDEvent
        fields = ["device", "card", "developer", "result", "uid"]

    def filter_uid(self, queryset, name, value):
        return queryset.filter(uid=normalize_uid(value))


class RFIDDeviceFilter(django_filters.FilterSet):
    online = django_filters.BooleanFilter(method="filter_online")

    class Meta:
        model = RFIDDevice
        fields = ["is_active", "purpose", "building", "online"]

    def filter_online(self, queryset, name, value):
        from datetime import timedelta

        from django.conf import settings
        from django.utils import timezone

        since = timezone.now() - timedelta(seconds=settings.RFID_DEVICE_OFFLINE_AFTER_SECONDS)
        online = queryset.filter(last_seen_at__gte=since)
        return online if value else queryset.exclude(pk__in=online.values("pk"))
