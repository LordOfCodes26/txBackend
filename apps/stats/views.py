from datetime import timedelta

from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.rfid.scope import building_scope
from common.permissions import HasPermissions

from .services import company_stats, finance_stats

MAX_DAYS = 366


class PeriodSerializer(serializers.Serializer):
    date_from = serializers.DateField(required=False, help_text="Default: 29 days before date_to.")
    date_to = serializers.DateField(required=False, help_text="Default: today (company time).")

    def validate(self, attrs):
        last = attrs.get("date_to") or timezone.localdate()
        first = attrs.get("date_from") or last - timedelta(days=29)
        if first > last:
            raise serializers.ValidationError(
                {"date_from": [_("The start date must not be after the end date.")]}
            )
        if (last - first).days + 1 > MAX_DAYS:
            raise serializers.ValidationError(
                {"date_from": [_("At most %(days)s days at once.") % {"days": MAX_DAYS}]}
            )
        return {"date_from": first, "date_to": last}


class FinanceStatsQuerySerializer(PeriodSerializer):
    building = serializers.IntegerField(
        required=False, min_value=1, help_text="Only this building's figures (a building id)."
    )

    def validate(self, attrs):
        building = attrs.get("building")
        attrs = super().validate(attrs)
        if building is not None:
            attrs["building"] = building
        return attrs


def chosen_buildings(user, permission: str, building: int | None):
    """The buildings the figures are limited to: the user's own limit (building owners and
    managers), narrowed to `building` when one is chosen. A building outside the user's
    limit gives no figures (an empty limit), not an error."""
    scope = building_scope(user, permission)
    if building is None:
        return scope
    if scope is not None and building not in scope:
        return frozenset()
    return frozenset({building})


class CompanyStatsView(APIView):
    """Company statistics for the BOSS dashboard (`stats.view`): developers, who is
    inside now, daily attendance, developer money (balances, deposits, spending) and
    store sales, for a period of company-local days.
    Building owners get the same figures limited to their buildings (`buildings`)."""

    permission_classes = [HasPermissions]
    required_permissions = {"get": ["stats.view"]}

    @extend_schema(parameters=[PeriodSerializer], responses=OpenApiTypes.OBJECT)
    def get(self, request):
        period = PeriodSerializer(data=request.query_params)
        period.is_valid(raise_exception=True)
        data = period.validated_data
        buildings = building_scope(request.user, "stats.view")  # building owners: theirs
        return Response(company_stats(data["date_from"], data["date_to"], buildings))


class FinanceStatsView(APIView):
    """Finance dashboard (`finance.stats.view`): developer money (balances, deposits,
    spending) and a per-seller comparison of sales and bookings, for a period of
    company-local days.
    Building owners get the figures of their buildings (`buildings`); `building` narrows
    them to one building (its developers' money, the sales at its counters)."""

    permission_classes = [HasPermissions]
    required_permissions = {"get": ["finance.stats.view"]}

    @extend_schema(parameters=[FinanceStatsQuerySerializer], responses=OpenApiTypes.OBJECT)
    def get(self, request):
        query = FinanceStatsQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = query.validated_data
        buildings = chosen_buildings(request.user, "finance.stats.view", data.get("building"))
        return Response(finance_stats(data["date_from"], data["date_to"], buildings))
