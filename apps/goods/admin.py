from django.contrib import admin

from .models import Good, GoodImage, InventoryMovement


class GoodImageInline(admin.TabularInline):
    model = GoodImage
    extra = 0
    readonly_fields = ["image", "alt_text", "position"]
    can_delete = False


@admin.register(Good)
class GoodAdmin(admin.ModelAdmin):
    """Read-only: prices and stock change through the API so they are audited."""

    list_display = [
        "name",
        "kind",
        "service_position",
        "price",
        "quantity",
        "is_active",
        "deleted_at",
    ]
    list_filter = ["kind", "is_active", "track_stock"]
    search_fields = ["name", "sku", "service_position__seller__name"]
    inlines = [GoodImageInline]

    def get_queryset(self, request):
        return Good.all_objects.select_related("service_position__seller")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(InventoryMovement)
class InventoryMovementAdmin(admin.ModelAdmin):
    list_display = ["created_at", "good", "kind", "quantity_delta", "quantity_after", "actor"]
    list_filter = ["kind"]
    search_fields = ["good__name", "reference"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
