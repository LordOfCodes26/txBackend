from django.contrib import admin

from .models import Purchase, PurchaseItem


class PurchaseItemInline(admin.TabularInline):
    model = PurchaseItem
    extra = 0
    can_delete = False
    readonly_fields = ["good", "quantity", "unit_price", "line_total"]


@admin.register(Purchase)
class PurchaseAdmin(admin.ModelAdmin):
    list_display = ["id", "created_at", "seller", "developer", "status", "total"]
    list_filter = ["status", "seller"]
    search_fields = ["developer__full_name", "seller__name"]
    inlines = [PurchaseItemInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
