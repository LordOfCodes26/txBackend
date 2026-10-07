"""Staging-only test console: a page to exercise door attendance and the till end to end.

Enabled by TEST_CONSOLE_ENABLED (off by default; never enable it in production).
The page itself is static; it talks to the normal API. Browsers can't open raw TCP
connections, so door scans go through `door_scan` below, which skips only the transport
authentication (fixed IP) and otherwise runs the same validation and pipeline as a real
door. Till taps run the real pipeline on the purchase's reader (`SimulateTapView`), as if
the seller pressed "Scan card to buy" and the developer tapped.
"""

from django.conf import settings
from django.http import Http404
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.purchases import services as purchases
from apps.purchases.models import Purchase, PurchaseStatus
from apps.rfid import services
from apps.rfid.models import DevicePurpose, RFIDCardAssignment, RFIDDevice, normalize_uid
from apps.rfid.serializers import ScanResponseSerializer, ScanSerializer
from apps.sellers.access import acting_seller
from common.permissions import HasPermissions


def _ensure_enabled():
    if not settings.TEST_CONSOLE_ENABLED:
        raise Http404()


def console_page(request):
    _ensure_enabled()
    return render(request, "common/test_console.html")


class SimulatedDoorScanView(APIView):
    """Simulate a door scan as if `door` had sent `{"ID", "Type", "UID"}` over TCP."""

    permission_classes = [HasPermissions]
    required_permissions = {"post": ["reader.manage"]}

    @extend_schema(
        request=inline_serializer(
            "SimulatedDoorScan",
            {
                "door": serializers.CharField(),
                "type": serializers.CharField(),
                "uid": serializers.CharField(),
            },
        ),
        responses=ScanResponseSerializer,
    )
    def post(self, request):
        _ensure_enabled()
        code = str(request.data.get("door", "")).strip()
        device = RFIDDevice.objects.filter(
            code__iexact=code, purpose=DevicePurpose.ATTENDANCE, is_active=True
        ).first()
        if device is None:
            raise NotFound(_("No active door with this code."))
        payload = {
            "ID": device.code,
            "Type": request.data.get("type"),
            "UID": request.data.get("uid"),
        }
        serializer = ScanSerializer(data=payload, context={"device": device})
        serializer.is_valid(raise_exception=True)
        event, _created = services.record_scan(
            device=device,
            uid=serializer.validated_data["uid"],
            direction=serializer.validated_data["direction"],
        )
        return Response(ScanResponseSerializer(event).data, status=201)


def _purchase_for(request, purchase_id) -> Purchase:
    """The purchase, if the caller may handle it (its own seller, or purchase.* staff)."""
    purchase = Purchase.objects.filter(pk=purchase_id).select_related("reader").first()
    if purchase is None:
        raise NotFound(_("No such purchase."))
    user = request.user
    seller = acting_seller(user)
    if not (user.has_rbac_perm("purchase.create") or (seller and seller.pk == purchase.seller_id)):
        raise NotFound(_("No such purchase."))
    return purchase


def _simulator_reader(user, seller) -> RFIDDevice:
    """A personal simulated till reader of the purchase's seller, so concurrent testers
    don't get each other's taps."""
    reader, created = RFIDDevice.objects.get_or_create(
        code=f"SIM-{user.pk}-{seller.pk}",
        defaults={
            "purpose": DevicePurpose.TILL,
            "seller": seller,
            "name": f"Simulated reader of {user.username}",
        },
    )
    if created:
        reader.set_new_api_key()
        reader.save(update_fields=["api_key_prefix", "api_key_hash"])
    return reader


class SimulateTapView(APIView):
    """Simulate a developer tapping their card on the purchase's till reader.

    Body: `{"purchase": id, "developer": id}` (uses their active card) or
    `{"purchase": id, "uid": "04..."}` (any UID, e.g. an unknown or blocked card).
    Runs the real tap pipeline (record, attach to the purchase, push `card_tapped`) and
    returns what a reader would get. A purchase without a reader is given the caller's
    personal simulated reader `SIM-<user id>`.
    """

    @extend_schema(
        request=inline_serializer(
            "SimulatedTap",
            {
                "purchase": serializers.IntegerField(),
                "developer": serializers.IntegerField(required=False),
                "uid": serializers.CharField(required=False),
            },
        ),
        responses=ScanResponseSerializer,
    )
    def post(self, request):
        _ensure_enabled()
        purchase = _purchase_for(request, request.data.get("purchase"))
        if purchase.status != PurchaseStatus.DRAFT:
            raise ValidationError({"purchase": [_("Only draft purchases can be paid.")]})
        uid = str(request.data.get("uid") or "").strip()
        if not uid:
            assignment = (
                RFIDCardAssignment.objects.filter(
                    developer_id=request.data.get("developer"), unassigned_at__isnull=True
                )
                .select_related("card")
                .first()
            )
            if assignment is None:
                raise ValidationError(
                    {"developer": [_("This developer has no active card; send a `uid` instead.")]}
                )
            uid = assignment.card.uid
        reader = purchase.reader
        if reader is None:
            reader = _simulator_reader(request.user, purchase.seller)
            purchases.set_reader(purchase=purchase, reader=reader)
        purchases.wait_for_card(purchase=purchase)  # the seller pressed "Scan card to buy"
        event, _created = services.record_scan(device=reader, uid=normalize_uid(uid))
        return Response(ScanResponseSerializer(event).data, status=201)


class TestCardsView(APIView):
    """Developers to test with: card UID, balance, status and whether a PIN is set.
    `?search=` filters by name or employee number (max 50 rows)."""

    @extend_schema(
        responses=inline_serializer(
            "TestCard",
            {
                "developer": serializers.IntegerField(),
                "employee_number": serializers.CharField(),
                "full_name": serializers.CharField(),
                "developer_status": serializers.CharField(),
                "card_uid": serializers.CharField(),
                "card_status": serializers.CharField(),
                "balance": serializers.CharField(allow_null=True),
                "account_status": serializers.CharField(allow_null=True),
                "has_pin": serializers.BooleanField(),
            },
            many=True,
        )
    )
    def get(self, request):
        _ensure_enabled()
        if not (request.user.has_rbac_perm("purchase.create") or acting_seller(request.user)):
            raise NotFound()
        qs = (
            RFIDCardAssignment.objects.filter(unassigned_at__isnull=True)
            .select_related("card", "developer__account")
            .order_by("developer__full_name")
        )
        term = request.query_params.get("search", "").strip()
        if term:
            qs = qs.filter(developer__full_name__icontains=term) | qs.filter(
                developer__employee_number__icontains=term
            )
        rows = []
        for a in qs[:50]:
            account = getattr(a.developer, "account", None)
            rows.append(
                {
                    "developer": a.developer.pk,
                    "employee_number": a.developer.employee_number,
                    "full_name": a.developer.full_name,
                    "developer_status": a.developer.status,
                    "card_uid": a.card.uid,
                    "card_status": a.card.status,
                    "balance": str(account.balance) if account else None,
                    "account_status": account.status if account else None,
                    "has_pin": bool(account and account.pin_hash),
                }
            )
        return Response(rows)
