from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class AttendanceConfig(AppConfig):
    name = "apps.attendance"
    label = "attendance"

    def ready(self):
        from .rules import RULES

        if settings.ATTENDANCE_DIRECTION_RULE not in RULES:
            raise ImproperlyConfigured(
                f"ATTENDANCE_DIRECTION_RULE must be one of {sorted(RULES)}, "
                f"got {settings.ATTENDANCE_DIRECTION_RULE!r}."
            )
        if not 0 <= settings.ATTENDANCE_DAY_START_HOUR <= 23:
            raise ImproperlyConfigured("ATTENDANCE_DAY_START_HOUR must be between 0 and 23.")
