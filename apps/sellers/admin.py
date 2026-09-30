from django.contrib import admin

from .models import Seller, ServicePosition


@admin.register(Seller)
class SellerAdmin(admin.ModelAdmin):
    list_display = ["name", "contact_name", "status", "user"]
    list_filter = ["status"]
    search_fields = ["name", "contact_name", "email"]
    raw_id_fields = ["user"]


@admin.register(ServicePosition)
class ServicePositionAdmin(admin.ModelAdmin):
    list_display = ["name", "seller", "location", "is_active", "deleted_at"]
    list_filter = ["is_active"]
    search_fields = ["name", "seller__name"]

    def get_queryset(self, request):
        return ServicePosition.all_objects.select_related("seller")
