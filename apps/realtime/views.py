from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from .tickets import TICKET_TTL, issue_ticket


class TicketView(APIView):
    """Get a single-use ticket (valid 30 s) to open a WebSocket:
    `wss://<host>/ws/counters/<service_position_id>/?ticket=<ticket>`."""

    @extend_schema(
        request=None,
        responses=inline_serializer(
            "RealtimeTicket",
            {"ticket": serializers.CharField(), "expires_in": serializers.IntegerField()},
        ),
    )
    def post(self, request):
        return Response({"ticket": issue_ticket(request.user), "expires_in": TICKET_TTL})
