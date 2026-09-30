from rest_framework import viewsets

from common.permissions import HasPermissions

from .filters import AuditLogFilter
from .models import AuditLog
from .serializers import AuditLogSerializer


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = AuditLog.objects.all()
    serializer_class = AuditLogSerializer
    permission_classes = [HasPermissions]
    required_permissions = {"list": ["audit.view"], "retrieve": ["audit.view"]}
    filterset_class = AuditLogFilter
    search_fields = ["actor_email", "action", "entity_id"]
    ordering_fields = ["created_at", "id"]
