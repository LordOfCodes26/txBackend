"""Who is inside which building right now.

A developer is inside building X when their **latest** non-void attendance record (on any
day) is an IN at one of X's door devices. Someone who never scanned out stays inside
until their next scan, or until a manager adds a manual OUT correction. Every developer
is counted at most once, so the total equals the sum over buildings (plus
`unknown_building` for an IN without a building, e.g. a manual correction). Deleted and
terminated developers are not counted.

The answer is kept in `DeveloperPresence` (one row per developer), refreshed by
`refresh_presence` whenever a developer's records change.

Requires IN/OUT labels, i.e. ATTENDANCE_DIRECTION_RULE = device (or toggle).
"""

from django.db.models import Count
from django.utils import timezone

from apps.developers.models import DeveloperStatus
from apps.rfid.models import Building

from .models import AttendanceRecord, DeveloperPresence, EventType


def refresh_presence(developer_id: int) -> DeveloperPresence:
    """Recompute one developer's presence from their latest record (indexed lookup)."""
    latest = (
        AttendanceRecord.objects.select_related("device")
        .filter(developer_id=developer_id, is_void=False)
        .order_by("-event_time", "-id")
        .first()
    )
    inside_now = latest is not None and latest.event_type == EventType.IN
    presence, _ = DeveloperPresence.objects.update_or_create(
        developer_id=developer_id,
        defaults={
            "is_inside": inside_now,
            "building_id": latest.device.building_id if inside_now and latest.device else None,
            "since": latest.event_time if latest else None,
            "record": latest,
        },
    )
    return presence


def refresh_all() -> int:
    ids = set(AttendanceRecord.objects.values_list("developer_id", flat=True).distinct())
    ids |= set(DeveloperPresence.objects.values_list("developer_id", flat=True))
    for developer_id in ids:
        refresh_presence(developer_id)
    return len(ids)


def inside():
    """Presence rows of everyone currently inside (excluding deleted/terminated)."""
    return (
        DeveloperPresence.objects.filter(
            is_inside=True,
            developer__deleted_at__isnull=True,
        )
        .exclude(developer__status=DeveloperStatus.TERMINATED)
        .select_related("developer", "building", "record__device")
    )


def occupancy(buildings: frozenset[int] | None = None) -> dict:
    """Counts per building and in total. `buildings` limits it to those buildings (a
    building manager's view: other buildings and `unknown_building` are left out)."""
    data = _occupancy()
    if buildings is None:
        return data
    data["buildings"] = [b for b in data["buildings"] if b["id"] in buildings]
    data["total"] = sum(b["count"] for b in data["buildings"])
    data["unknown_building"] = 0
    return data


def _occupancy() -> dict:
    counts = {
        row["building"]: row["n"]
        for row in inside().order_by().values("building").annotate(n=Count("pk"))
    }
    # Active developers whose latest scan was at this building, still inside or already left.
    rosters = {
        row["record__device__building"]: row["n"]
        for row in (
            DeveloperPresence.objects.filter(developer__deleted_at__isnull=True)
            .exclude(developer__status=DeveloperStatus.TERMINATED)
            .order_by()
            .values("record__device__building")
            .annotate(n=Count("pk"))
        )
    }
    buildings = [
        {
            "id": b.pk,
            "code": b.code,
            "name": b.name,
            "count": counts.get(b.pk, 0),
            "developers": rosters.get(b.pk, 0),
        }
        for b in Building.objects.all()
    ]
    return {
        "as_of": timezone.now().isoformat(),
        "total": sum(counts.values()),
        "buildings": buildings,
        "unknown_building": counts.get(None, 0),
    }
