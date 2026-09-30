import re
import uuid

from django.conf import settings

from .context import RequestContext, reset_request_context, set_request_context

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def client_ip(request) -> str | None:
    if settings.TRUST_X_FORWARDED_FOR:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


class RequestContextMiddleware:
    """Assigns a request id and exposes ip/user-agent to logging and the audit log."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.headers.get("X-Request-ID", "")
        request_id = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        request.request_id = request_id
        token = set_request_context(
            RequestContext(
                request_id=request_id,
                ip_address=client_ip(request),
                user_agent=request.headers.get("User-Agent", "")[:512],
            )
        )
        try:
            response = self.get_response(request)
        finally:
            reset_request_context(token)
        response["X-Request-ID"] = request_id
        return response
