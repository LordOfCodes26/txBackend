from django.contrib import admin

from .models import SellerAccount, SellerPayment, SellerTransaction


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SellerAccount)
class SellerAccountAdmin(ReadOnlyAdmin):
    list_display = ["seller", "balance", "updated_at"]
    search_fields = ["seller__name"]


@admin.register(SellerTransaction)
class SellerTransactionAdmin(ReadOnlyAdmin):
    list_display = ["created_at", "account", "kind", "amount", "balance_after", "reference"]
    list_filter = ["kind"]
    search_fields = ["account__seller__name", "reference"]


@admin.register(SellerPayment)
class SellerPaymentAdmin(ReadOnlyAdmin):
    list_display = ["id", "created_at", "seller", "amount", "status", "payment_reference"]
    list_filter = ["status"]
    search_fields = ["seller__name", "payment_reference"]
