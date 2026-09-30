import django_filters

from .models import AuditLog


class AuditLogFilter(django_filters.FilterSet):
    created_after = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_before = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="lt")

    class Meta:
        model = AuditLog
        fields = ["action", "entity_type", "entity_id", "actor"]
