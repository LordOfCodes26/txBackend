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


class CompanyStatsView(APIView):
    """Company statistics for the BOSS dashboard (`stats.view`): developers, who is
    inside now, daily attendance, developer money (balances, deposits, spending) and
    seller money (earnings, payouts) and store sales, for a period of company-local days.
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
    """Finance dashboard (`finance.view` + `seller_finance.view`): developer money
    (balances, deposits, spending), seller money and a per-seller comparison of sales,
    bookings, earnings, payouts and balances, for a period of company-local days.
    Building owners get the figures of their buildings (`buildings`)."""

    permission_classes = [HasPermissions]
    required_permissions = {"get": ["finance.view", "seller_finance.view"]}

    @extend_schema(parameters=[PeriodSerializer], responses=OpenApiTypes.OBJECT)
    def get(self, request):
        period = PeriodSerializer(data=request.query_params)
        period.is_valid(raise_exception=True)
        data = period.validated_data
        buildings = building_scope(request.user, "seller_finance.view")
        return Response(finance_stats(data["date_from"], data["date_to"], buildings))
