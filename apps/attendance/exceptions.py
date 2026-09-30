from rest_framework import status

from common.exceptions import DomainError


class RecordAlreadyVoid(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "RECORD_ALREADY_VOID"
    default_detail = "This attendance record is already void."
