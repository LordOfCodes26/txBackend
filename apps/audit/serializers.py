from rest_framework import serializers

from .models import AuditLog


class AuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditLog
        fields = [
            "id",
            "actor",
            "actor_email",
            "action",
            "entity_type",
            "entity_id",
            "old_values",
            "new_values",
            "ip_address",
            "user_agent",
            "request_id",
            "created_at",
        ]
