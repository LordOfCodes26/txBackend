from django.contrib import admin

from apps.finance.admin import ReadOnlyAdmin

from .models import CashEntry, StockCount


@admin.register(CashEntry)
class CashEntryAdmin(ReadOnlyAdmin):
    list_display = ["created_at", "seller", "kind", "amount", "owed_after", "actor"]
    list_filter = ["kind", "seller"]
    search_fields = ["reference", "note"]


@admin.register(StockCount)
class StockCountAdmin(ReadOnlyAdmin):
    list_display = ["created_at", "seller", "missing_value", "surplus_value", "outcome"]
    list_filter = ["outcome", "seller"]
