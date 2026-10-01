from django.conf import settings
from django.db import models
from django.db.models import Q

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
    phone = models.CharField(max_length=50, blank=True)
    home_address = models.TextField(blank=True)
    birthday = models.DateField(null=True, blank=True)
    department = models.CharField(max_length=100, blank=True, db_index=True)
    position_title = models.CharField(max_length=100, blank=True)
    manager = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="reports"
    )
    start_date = models.DateField(null=True, blank=True)
    out_date = models.DateField(
        null=True, blank=True, help_text="Last working day (set when the developer leaves)."
    )
    status = models.CharField(
        max_length=20, choices=DeveloperStatus.choices, default=DeveloperStatus.ACTIVE
    )

    class Meta:
        ordering = ["full_name", "id"]
        constraints = [
            models.UniqueConstraint(
                "employee_number", condition=ALIVE, name="developer_employee_number_unique"
            ),
            models.UniqueConstraint("user", condition=ALIVE, name="developer_user_unique"),
            models.CheckConstraint(
                condition=~Q(manager=models.F("id")), name="developer_not_own_manager"
            ),
            models.CheckConstraint(
                condition=Q(out_date__isnull=True)
                | Q(start_date__isnull=True)
                | Q(out_date__gte=models.F("start_date")),
                name="developer_out_date_after_start",
            ),
        ]
        indexes = [models.Index(fields=["status", "department"])]

    def __str__(self):
        return f"{self.employee_number} {self.full_name}"
