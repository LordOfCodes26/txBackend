"""How a day's scans are labelled IN/OUT and turned into worked time.

Selected with the ATTENDANCE_DIRECTION_RULE setting. Changing it only affects days that
are recalculated, so run `manage.py rebuild_attendance` afterwards.

  none    Scans are not labelled. Worked time = last scan - first scan.
  toggle  Scans alternate IN, OUT, IN, ... in time order.
  device  Scans take the reader's direction (entrance = IN, exit = OUT); readers
          set to BOTH (and manual records) fall back to toggling.
"""

from datetime import datetime

from apps.rfid.models import DeviceDirection

from .models import EventType


def _toggle_after(previous: str | None) -> str:
    return EventType.OUT if previous == EventType.IN else EventType.IN


def classify_none(records) -> list[str]:
    return [EventType.SCAN] * len(records)


def classify_toggle(records) -> list[str]:
    return [EventType.IN if i % 2 == 0 else EventType.OUT for i in range(len(records))]


def classify_device(records) -> list[str]:
    types: list[str] = []
    for record in records:
        direction = record.device.direction if record.device else DeviceDirection.BOTH
        if direction == DeviceDirection.IN:
            types.append(EventType.IN)
        elif direction == DeviceDirection.OUT:
            types.append(EventType.OUT)
        else:
            types.append(_toggle_after(types[-1] if types else None))
    return types


RULES = {"none": classify_none, "toggle": classify_toggle, "device": classify_device}


def worked_time(times: list[datetime], types: list[str]) -> tuple[int, bool]:
    """Return (worked seconds, complete). `times` is sorted ascending."""
    if not times:
        return 0, False
    if all(t == EventType.SCAN for t in types):
        if len(times) < 2:
            return 0, False
        return int((times[-1] - times[0]).total_seconds()), True

    worked, complete, open_in = 0, True, None
    for moment, kind in zip(times, types, strict=True):
        if kind == EventType.IN:
            if open_in is not None:
                complete = False  # IN twice in a row: keep the earlier one
            else:
                open_in = moment
        elif kind == EventType.OUT:
            if open_in is None:
                complete = False  # OUT without a matching IN
            else:
                worked += int((moment - open_in).total_seconds())
                open_in = None
    if open_in is not None:
        complete = False
    return worked, complete
