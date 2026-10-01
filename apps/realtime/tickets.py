"""Single-use, short-lived tickets for opening a WebSocket from a browser.

Browsers cannot send an Authorization header on a WebSocket, and putting the JWT in the
URL would leak it into proxy logs. Instead the page asks the REST API for a ticket
(authenticated as usual) and connects with `?ticket=...` within TICKET_TTL seconds.
"""

import secrets

from django.core.cache import cache

TICKET_TTL = 30
_PREFIX = "ws-ticket:"


def issue_ticket(user) -> str:
    ticket = secrets.token_urlsafe(32)
    cache.set(_PREFIX + ticket, user.pk, timeout=TICKET_TTL)
    return ticket


def redeem_ticket(ticket: str) -> int | None:
    """Return the user id and invalidate the ticket (it works once)."""
    if not ticket:
        return None
    key = _PREFIX + ticket
    user_id = cache.get(key)
    if user_id is not None:
        cache.delete(key)
    return user_id
