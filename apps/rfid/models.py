import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from apps.developers.models import Developer
from common.models import AppendOnlyModel, TimeStampedModel

uid_validator = RegexValidator(r"^[0-9A-F]{4,32}$", "UID must be 4-32 hexadecimal characters.")


def normalize_uid(raw: str) -> str:
    """Readers format UIDs differently ("04:aa:bb", "04-AA-BB", "04 aa bb")."""
    return "".join(ch for ch in str(raw) if ch not in ":- ").upper()


class CardStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    BLOCKED = "BLOCKED", "Blocked"
    RETIRED = "RETIRED", "Retired"


class RFIDCard(TimeStampedModel):
    """A physical card. ACTIVE cards can be assigned and scanned; BLOCKED cards keep their
    owner but every scan is rejected; RETIRED cards are out of circulation for good."""

    uid = models.CharField(max_length=32, unique=True, validators=[uid_validator])
    label = models.CharField(max_length=50, blank=True, help_text="Number printed on the card.")
    status = models.CharField(max_length=10, choices=CardStatus.choices, default=CardStatus.ACTIVE)
    status_reason = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["uid"]
        verbose_name = "RFID card"

    def __str__(self):
        return self.label or self.uid

    @property
    def current_assignment(self):
        # Uses the prefetch from views when present.
        for assignment in self.assignments.all():
            if assignment.unassigned_at is None:
                return assignment
        return None


class AssignmentEndReason(models.TextChoices):
    RETURNED = "RETURNED", "Returned"
    REPLACED = "REPLACED", "Replaced"
    DEVELOPER_LEFT = "DEVELOPER_LEFT", "Developer left"
    DEVELOPER_DELETED = "DEVELOPER_DELETED", "Developer record deleted"


class RFIDCardAssignment(models.Model):
    """Who held which card, when. Active rows have `unassigned_at IS NULL`."""

    card = models.ForeignKey(RFIDCard, on_delete=models.PROTECT, related_name="assignments")
    developer = models.ForeignKey(
        Developer, on_delete=models.PROTECT, related_name="card_assignments"
    )
    assigned_at = models.DateTimeField(default=timezone.now)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, related_name="+"
    )
    unassigned_at = models.DateTimeField(null=True, blank=True)
    unassigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    end_reason = models.CharField(max_length=20, choices=AssignmentEndReason.choices, blank=True)

    class Meta:
        ordering = ["-assigned_at", "-id"]
        verbose_name = "RFID card assignment"
        constraints = [
            models.UniqueConstraint(
                "card", condition=Q(unassigned_at__isnull=True), name="rfid_one_owner_per_card"
            ),
            models.UniqueConstraint(
                "developer",
                condition=Q(unassigned_at__isnull=True),
                name="rfid_one_card_per_developer",
            ),
            models.CheckConstraint(
                condition=Q(unassigned_at__isnull=True) | Q(unassigned_at__gte=F("assigned_at")),
                name="rfid_assignment_ends_after_start",
            ),
        ]

    def __str__(self):
        return f"{self.card} → {self.developer}"


class DevicePurpose(models.TextChoices):
    ATTENDANCE = "ATTENDANCE", "Attendance reader"
    TILL = "TILL", "Till card reader (program on a seller's computer)"


class Building(TimeStampedModel):
    """A company building. Its attendance door devices tell who is inside."""

    code = models.CharField(max_length=20, unique=True, help_text="e.g. B1")
    name = models.CharField(max_length=100, unique=True, help_text="e.g. Building 1")

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return self.name


class DeviceDirection(models.TextChoices):
    IN = "IN", "Entrance"
    OUT = "OUT", "Exit"
    BOTH = "BOTH", "Entrance and exit"


class RFIDDevice(TimeStampedModel):
    """A networked reader. It authenticates with `Authorization: Device <api key>`.

    Only a SHA-256 hash of the key is stored (the key is 256 random bits, so a slow
    password hash adds nothing); the plaintext is shown once on creation/rotation.
    """

    code = models.CharField(max_length=50, unique=True, help_text="e.g. READER-001")
    name = models.CharField(max_length=100, blank=True)
    location = models.CharField(max_length=255, blank=True)
    purpose = models.CharField(
        max_length=20, choices=DevicePurpose.choices, default=DevicePurpose.ATTENDANCE
    )
    direction = models.CharField(
        max_length=4,
        choices=DeviceDirection.choices,
        default=DeviceDirection.BOTH,
        help_text="Used by the attendance 'device' rule to label scans IN/OUT.",
    )
    is_active = models.BooleanField(default=True)
    api_key_prefix = models.CharField(max_length=8, db_index=True, editable=False)
    api_key_hash = models.CharField(max_length=64, editable=False)
    building = models.ForeignKey(
        Building,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="devices",
        help_text="ATTENDANCE devices: the building whose door this is (for occupancy).",
    )
    service_position = models.ForeignKey(
        "sellers.ServicePosition",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="till_devices",
        help_text="TILL devices only: the counter whose purchases receive this reader's taps.",
    )
    allowed_ip = models.GenericIPAddressField(
        null=True,
        blank=True,
        help_text=(
            "ATTENDANCE doors that cannot send an API key: requests from this fixed IP that "
            "carry this device's code as `ID` are accepted without a key."
        ),
    )
    last_seen_at = models.DateTimeField(null=True, blank=True)
    last_ip = models.GenericIPAddressField(null=True, blank=True)
    app_version = models.CharField(
        max_length=50, blank=True, help_text="Firmware or program version, from heartbeats."
    )

    class Meta:
        ordering = ["code"]
        constraints = [
            models.CheckConstraint(
                condition=Q(purpose=DevicePurpose.ATTENDANCE) | Q(building__isnull=True),
                name="rfid_only_attendance_devices_have_building",
            ),
            models.CheckConstraint(
                condition=Q(purpose=DevicePurpose.ATTENDANCE) | Q(allowed_ip__isnull=True),
                name="rfid_only_attendance_devices_use_ip_auth",
            ),
            models.CheckConstraint(
                condition=(
                    Q(purpose=DevicePurpose.TILL, service_position__isnull=False)
                    | (~Q(purpose=DevicePurpose.TILL) & Q(service_position__isnull=True))
                ),
                name="rfid_till_device_has_position",
            ),
        ]
        verbose_name = "RFID device"

    def __str__(self):
        return self.code

    @property
    def is_online(self) -> bool:
        if self.last_seen_at is None:
            return False
        limit = timezone.now() - timedelta(seconds=settings.RFID_DEVICE_OFFLINE_AFTER_SECONDS)
        return self.last_seen_at >= limit

    @staticmethod
    def hash_key(key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()

    def set_new_api_key(self) -> str:
        key = secrets.token_urlsafe(32)
        self.api_key_prefix = key[:8]
        self.api_key_hash = self.hash_key(key)
        return key

    def check_api_key(self, key: str) -> bool:
        return secrets.compare_digest(self.api_key_hash, self.hash_key(key))


class ScanDirection(models.TextChoices):
    IN = "IN", "In"
    OUT = "OUT", "Out"


class ScanResult(models.TextChoices):
    ACCEPTED = "ACCEPTED", "Accepted"
    DUPLICATE = "DUPLICATE", "Duplicate (debounced)"
    UNKNOWN_CARD = "UNKNOWN_CARD", "Unknown card"
    UNASSIGNED_CARD = "UNASSIGNED_CARD", "Card not assigned"
    BLOCKED_CARD = "BLOCKED_CARD", "Card blocked"
    RETIRED_CARD = "RETIRED_CARD", "Card retired"
    INACTIVE_DEVELOPER = "INACTIVE_DEVELOPER", "Developer not active"


class RFIDEvent(AppendOnlyModel):
    """One raw scan, stored exactly once and never modified.

    `event_time` is the reader's clock, `received_at` the server's; both are kept
    because readers can buffer scans while the network is down.
    """

    device = models.ForeignKey(RFIDDevice, on_delete=models.PROTECT, related_name="events")
    client_event_id = models.CharField(
        max_length=64,
        blank=True,
        help_text="Reader-generated id; a retried request with the same id is not stored twice.",
    )
    uid = models.CharField(max_length=32)
    card = models.ForeignKey(
        RFIDCard, on_delete=models.PROTECT, null=True, blank=True, related_name="events"
    )
    developer = models.ForeignKey(
        Developer, on_delete=models.PROTECT, null=True, blank=True, related_name="rfid_events"
    )
    event_time = models.DateTimeField()
    received_at = models.DateTimeField(default=timezone.now)
    direction = models.CharField(
        max_length=3,
        choices=ScanDirection.choices,
        blank=True,
        help_text="In/out as reported by the device with this scan (blank if not reported).",
    )
    result = models.CharField(max_length=20, choices=ScanResult.choices)

    class Meta:
        ordering = ["-event_time", "-id"]
        verbose_name = "RFID event"
        constraints = [
            models.UniqueConstraint(
                "device",
                "client_event_id",
                condition=~Q(client_event_id=""),
                name="rfid_event_client_id_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["card", "event_time"]),
            models.Index(fields=["developer", "event_time"]),
            models.Index(fields=["result", "event_time"]),
        ]

    def __str__(self):
        return f"{self.uid} @ {self.device_id} {self.result}"
