from django.contrib import admin

from .models import Developer


@admin.register(Developer)
class DeveloperAdmin(admin.ModelAdmin):
    list_display = [
        "employee_number",
        "full_name",
        "department",
        "status",
        "start_date",
        "out_date",
    ]
    list_filter = ["status", "department"]
    search_fields = ["employee_number", "full_name", "phone"]
    raw_id_fields = ["user", "manager"]

    def get_queryset(self, request):
        return Developer.all_objects.all()
