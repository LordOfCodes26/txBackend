import django_filters

from .models import AttendanceRecord, DailyAttendance


class AttendanceRecordFilter(django_filters.FilterSet):
    date_from = django_filters.DateFilter(field_name="work_date", lookup_expr="gte")
    date_to = django_filters.DateFilter(field_name="work_date", lookup_expr="lte")

    class Meta:
        model = AttendanceRecord
        fields = ["developer", "work_date", "source", "event_type", "is_void", "device"]


class DailyAttendanceFilter(django_filters.FilterSet):
    date_from = django_filters.DateFilter(field_name="work_date", lookup_expr="gte")
    date_to = django_filters.DateFilter(field_name="work_date", lookup_expr="lte")
    department = django_filters.CharFilter(field_name="developer__department", lookup_expr="iexact")

    class Meta:
        model = DailyAttendance
        fields = ["developer", "work_date", "status", "department"]
