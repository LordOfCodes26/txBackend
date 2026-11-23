from django.contrib import admin

from apps.finance.admin import ReadOnlyAdmin

from .models import CashEntry, CashRequest, StockCount


@admin.register(CashEntry)
class CashEntryAdmin(ReadOnlyAdmin):
    list_display = ["created_at", "seller", "kind", "amount", "owed_after", "actor"]
    list_filter = ["kind", "seller"]
    search_fields = ["reference", "note"]


@admin.register(CashRequest)
class CashRequestAdmin(ReadOnlyAdmin):
    list_display = ["created_at", "kind", "status", "seller", "requested_by", "decided_by"]
    list_filter = ["kind", "status", "seller"]


@admin.register(StockCount)
class StockCountAdmin(ReadOnlyAdmin):
    list_display = ["created_at", "seller", "missing_value", "surplus_value", "outcome"]
    list_filter = ["outcome", "seller"]
