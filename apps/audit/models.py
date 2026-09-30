from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils import timezone

from common.models import AppendOnlyModel


class AuditLog(AppendOnlyModel):
    """Immutable record of a sensitive action. UPDATE/DELETE are blocked by a DB trigger."""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        help_text="Null for system actions (Celery tasks, RFID devices, migrations).",
    )
    actor_email = models.CharField(max_length=254, blank=True)
    action = models.CharField(max_length=100, db_index=True)
    entity_type = models.CharField(max_length=100)
    entity_id = models.CharField(max_length=64)
    old_values = models.JSONField(null=True, blank=True, encoder=DjangoJSONEncoder)
    new_values = models.JSONField(null=True, blank=True, encoder=DjangoJSONEncoder)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    request_id = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["entity_type", "entity_id"]),
            models.Index(fields=["actor", "created_at"]),
        ]

    def __str__(self):
        return f"{self.action} {self.entity_type}:{self.entity_id}"
