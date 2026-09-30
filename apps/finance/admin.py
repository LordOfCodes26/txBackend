from django.contrib import admin

from .models import AccountTransaction, DeveloperAccount


class ReadOnlyAdmin(admin.ModelAdmin):
    """Money moves only through the API, so every change is validated and audited."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DeveloperAccount)
class DeveloperAccountAdmin(ReadOnlyAdmin):
    list_display = ["developer", "balance", "status", "updated_at"]
    list_filter = ["status"]
    search_fields = ["developer__full_name", "developer__employee_number"]


@admin.register(AccountTransaction)
class AccountTransactionAdmin(ReadOnlyAdmin):
    list_display = ["created_at", "account", "kind", "amount", "balance_after", "actor"]
    list_filter = ["kind"]
    search_fields = ["account__developer__full_name", "reference", "description"]
