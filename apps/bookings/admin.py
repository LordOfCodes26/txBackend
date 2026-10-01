from django.contrib import admin

from .models import Booking


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ["start", "end", "good", "developer", "slots", "purchase"]
    list_filter = ["good"]
    search_fields = ["developer__full_name", "good__name"]
    date_hierarchy = "start"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
