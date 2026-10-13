from django.db import transaction
from django.db.models import F
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.filters import OrderingFilter, SearchFilter
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from apps.finance.serializers import NewPinSerializer
from apps.finance.services import give_pin, open_account, validate_pin_format
from apps.rfid import services as rfid
from apps.rfid.models import CardStatus, RFIDCard
from apps.rfid.scope import BuildingScopedMixin, ensure_in_scope
from common.permissions import HasPermissions

from . import services
from .exceptions import DeveloperProfileNotFound
from .filters import DeveloperFilter
from .models import Developer
from .serializers import (
    DeveloperPhotoSerializer,
    DeveloperSerializer,
    MyDeveloperProfileSerializer,
)


class NewDeveloperCardSerializer(NewPinSerializer):
    """Optional on create: a card (tapped on a card assign reader) for the new developer,
    with the purchase PIN they type twice, as on the Assign card page."""

    card = serializers.PrimaryKeyRelatedField(queryset=RFIDCard.objects.all())

    def validate_card(self, card):
        if card.status != CardStatus.ACTIVE:
            raise serializers.ValidationError(_("Only active cards can be assigned."))
        if card.assignments.filter(unassigned_at__isnull=True).exists():
            raise serializers.ValidationError(_("This card is already assigned to a developer."))
        return card

    def validate(self, attrs):
        attrs = super().validate(attrs)
        validate_pin_format(attrs["pin"])
        return attrs


class EmptyLastOrdering(OrderingFilter):
    """Sorting where empty values (no card, no birthday, ...) come last both ways."""

    def filter_queryset(self, request, queryset, view):
        ordering = self.get_ordering(request, queryset, view)
        if not ordering:
            return queryset
        return queryset.order_by(
            *(
                F(field[1:]).desc(nulls_last=True)
                if field.startswith("-")
                else F(field).asc(nulls_last=True)
                for field in ordering
            )
        )


def with_card_label(developers):
    """Each developer's current card label ("" when the card has none, null without a card)."""
    from django.db.models import OuterRef, Subquery

    from apps.rfid.models import RFIDCardAssignment

    current = RFIDCardAssignment.objects.filter(
        developer=OuterRef("pk"), unassigned_at__isnull=True
    ).values("card__label")[:1]
    return developers.annotate(card_label=Subquery(current))


class DeveloperViewSet(BuildingScopedMixin, viewsets.ModelViewSet):
    """DELETE is a soft delete. For someone leaving the company, set `status=TERMINATED`.

    Building managers see and manage only developers of their buildings.
    """

    queryset = Developer.objects.select_related("building")
    serializer_class = DeveloperSerializer
    permission_classes = [HasPermissions]
    required_permissions = {
        "list": ["developer.view"],
        "retrieve": ["developer.view"],
        "create": ["developer.create"],
        "partial_update": ["developer.update"],
        "photo": ["developer.update"],
        "destroy": ["developer.delete"],
        "me": [],
        "departments": ["developer.view"],
    }
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    filterset_class = DeveloperFilter
    filter_backends = [DjangoFilterBackend, SearchFilter, EmptyLastOrdering]
    # `card_label`: the label of the card the developer holds now (annotated below), so a
    # card's number finds its holder.
    search_fields = [
        "full_name",
        "employee_number",
        "department",
        "position_title",
        "phone",
        "card_label",
    ]
    ordering_fields = [
        "full_name",
        "employee_number",
        "department",
        "start_date",
        "out_date",
        "birthday",
        "created_at",
        "card_label",
    ]

    def get_queryset(self):
        return with_card_label(super().get_queryset())

    @extend_schema(
        methods=["POST"], request=DeveloperPhotoSerializer, responses=DeveloperSerializer
    )
    @extend_schema(methods=["DELETE"], request=None, responses=DeveloperSerializer)
    @action(
        detail=True,
        methods=["post", "delete"],
        parser_classes=[MultiPartParser, FormParser],
    )
    def photo(self, request, pk=None):
        """POST (multipart field `photo`): set or replace the photo. DELETE: remove it."""
        developer = self.get_object()
        if request.method == "DELETE":
            developer = services.set_photo(actor=request.user, developer=developer, photo=None)
        else:
            body = DeveloperPhotoSerializer(data=request.data)
            body.is_valid(raise_exception=True)
            developer = services.set_photo(
                actor=request.user, developer=developer, photo=body.validated_data["photo"]
            )
        return Response(DeveloperSerializer(developer).data)

    def create(self, request, *args, **kwargs):
        """With `card`, `pin` and `pin_confirm`, the new developer also gets that card and
        purchase PIN, in the same transaction (needs `rfid.assign`)."""
        self._new_card = None
        if request.data.get("card"):
            if not request.user.has_rbac_perm("card.assign"):
                raise PermissionDenied()
            part = NewDeveloperCardSerializer(data=request.data)
            developer = self.get_serializer(data=request.data)
            card_ok, developer_ok = part.is_valid(), developer.is_valid()
            if not (card_ok and developer_ok):  # every problem at once, at its field
                raise serializers.ValidationError({**developer.errors, **part.errors})
            self._new_card = part.validated_data
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        ensure_in_scope(
            self.request.user,
            "developer.create",
            serializer.validated_data.get("building") and serializer.validated_data["building"].pk,
        )
        with transaction.atomic():
            serializer.instance = services.create_developer(
                actor=self.request.user, **serializer.validated_data
            )
            card = getattr(self, "_new_card", None)
            if card:
                rfid.assign_card(
                    actor=self.request.user, card=card["card"], developer=serializer.instance
                )
                give_pin(
                    actor=self.request.user,
                    account=open_account(serializer.instance),
                    pin=card["pin"],
                    action="finance.pin_set_at_card_assignment",
                )

    def perform_update(self, serializer):
        if "building" in serializer.validated_data:
            building = serializer.validated_data["building"]
            ensure_in_scope(self.request.user, "developer.update", building and building.pk)
        serializer.instance = services.update_developer(
            actor=self.request.user, developer=serializer.instance, **serializer.validated_data
        )

    def perform_destroy(self, instance):
        services.delete_developer(actor=self.request.user, developer=instance)

    @extend_schema(responses={200: {"type": "array", "items": {"type": "string"}}})
    @action(detail=False, methods=["get"])
    def departments(self, request):
        """The departments already used (sorted, no blanks), to pick from when typing one."""
        names = (
            self.get_queryset()
            .exclude(department="")
            .order_by("department")
            .values_list("department", flat=True)
            .distinct()
        )
        return Response(list(names))

    @extend_schema(responses=MyDeveloperProfileSerializer)
    @action(detail=False, methods=["get"])
    def me(self, request):
        developer = Developer.objects.filter(user=request.user).first()
        if developer is None:
            raise DeveloperProfileNotFound()
        return Response(MyDeveloperProfileSerializer(developer).data, status=status.HTTP_200_OK)
