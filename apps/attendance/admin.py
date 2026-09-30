from django.contrib import admin

from .models import AttendanceRecord, DailyAttendance


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AttendanceRecord)
class AttendanceRecordAdmin(ReadOnlyAdmin):
    list_display = ["work_date", "event_time", "developer", "event_type", "source", "is_void"]
    list_filter = ["source", "event_type", "is_void"]
    search_fields = ["developer__full_name", "developer__employee_number"]
    date_hierarchy = "work_date"


@admin.register(DailyAttendance)
class DailyAttendanceAdmin(ReadOnlyAdmin):
    list_display = ["work_date", "developer", "first_seen", "last_seen", "worked_seconds", "status"]
    list_filter = ["status"]
    search_fields = ["developer__full_name", "developer__employee_number"]
    date_hierarchy = "work_date"
