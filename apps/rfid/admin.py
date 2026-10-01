from django.contrib import admin

from .models import Building, RFIDCard, RFIDCardAssignment, RFIDDevice, RFIDEvent


class ReadOnlyAdmin(admin.ModelAdmin):
    """State changes go through the API so they are validated and audited."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(RFIDCard)
class RFIDCardAdmin(ReadOnlyAdmin):
    list_display = ["uid", "label", "status", "created_at"]
    list_filter = ["status"]
    search_fields = ["uid", "label"]


@admin.register(RFIDCardAssignment)
class RFIDCardAssignmentAdmin(ReadOnlyAdmin):
    list_display = ["card", "developer", "assigned_at", "unassigned_at", "end_reason"]
    list_filter = ["end_reason"]
    search_fields = ["card__uid", "developer__full_name"]


@admin.register(RFIDDevice)
class RFIDDeviceAdmin(ReadOnlyAdmin):
    list_display = ["code", "name", "location", "is_active", "last_seen_at"]


@admin.register(RFIDEvent)
class RFIDEventAdmin(ReadOnlyAdmin):
    list_display = ["event_time", "device", "uid", "developer", "result"]
    list_filter = ["result", "device"]
    search_fields = ["uid", "developer__full_name"]


@admin.register(Building)
class BuildingAdmin(admin.ModelAdmin):
    list_display = ["code", "name"]
