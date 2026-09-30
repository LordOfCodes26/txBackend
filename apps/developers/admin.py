from django.contrib import admin

from .models import Developer


@admin.register(Developer)
class DeveloperAdmin(admin.ModelAdmin):
    list_display = ["employee_number", "full_name", "email", "department", "status"]
    list_filter = ["status", "department"]
    search_fields = ["employee_number", "full_name", "email"]
    raw_id_fields = ["user", "manager"]

    def get_queryset(self, request):
        return Developer.all_objects.all()
