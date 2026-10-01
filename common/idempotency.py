import re

from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError

HEADER = "Idempotency-Key"
_VALID = re.compile(r"^[A-Za-z0-9_.:-]{8,64}$")


def require_idempotency_key(request) -> str:
    """Money-moving endpoints require a client-generated key (use a UUID per user action).

    Resending the same request with the same key returns the original result instead of
    moving money twice, which makes retries after a timeout or double-click safe.
    """
    key = request.headers.get(HEADER, "").strip()
    if not _VALID.match(key):
        raise ValidationError(
            {
                HEADER: [
                    _("Required header: 8-64 characters (letters, digits, _ . : -); use a UUID.")
                ]
            }
        )
    return key
