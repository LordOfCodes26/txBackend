from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from common.models import SoftDeleteModel, TimeStampedModel

ALIVE = Q(deleted_at__isnull=True)


class DeveloperStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    ON_LEAVE = "ON_LEAVE", "On leave"
    SUSPENDED = "SUSPENDED", "Suspended"
    TERMINATED = "TERMINATED", "Terminated"


class Developer(TimeStampedModel, SoftDeleteModel):
    """An employee who can hold an RFID card, a balance and make purchases.

    Leaving the company is `status=TERMINATED` (history stays visible). Soft delete is
    for records created by mistake.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="developer_profiles",
        help_text="Login account; optional until the developer needs self-service access.",
    )
    employee_number = models.CharField(max_length=50)
    full_name = models.CharField(max_length=255)
    email = models.EmailField()
    phone = models.CharField(max_length=50, blank=True)
    department = models.CharField(max_length=100, blank=True, db_index=True)
    position_title = models.CharField(max_length=100, blank=True)
    manager = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="reports"
    )
    start_date = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=20, choices=DeveloperStatus.choices, default=DeveloperStatus.ACTIVE
    )

    class Meta:
        ordering = ["full_name", "id"]
        constraints = [
            models.UniqueConstraint(
                "employee_number", condition=ALIVE, name="developer_employee_number_unique"
            ),
            models.UniqueConstraint(
                Lower("email"), condition=ALIVE, name="developer_email_ci_unique"
            ),
            models.UniqueConstraint("user", condition=ALIVE, name="developer_user_unique"),
            models.CheckConstraint(
                condition=~Q(manager=models.F("id")), name="developer_not_own_manager"
            ),
        ]
        indexes = [models.Index(fields=["status", "department"])]

    def __str__(self):
        return f"{self.employee_number} {self.full_name}"

    def save(self, *args, **kwargs):
        self.email = self.email.lower()
        super().save(*args, **kwargs)
