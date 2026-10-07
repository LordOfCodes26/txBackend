from django.http import HttpResponse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.stats.views import PeriodSerializer
from common.permissions import HasPermissions

from . import exports
from .imports import IMPORTERS, run_import
from .xlsx import XLSX, template

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _file(content: bytes, name: str) -> HttpResponse:
    response = HttpResponse(content, content_type=XLSX)
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    return response


def _importer(kind: str, user):
    importer = IMPORTERS.get(kind)
    if importer is None:
        raise NotFound()
    # Developers: either right is enough here; each row checks create or update.
    if not user.has_rbac_perm("excel.import") or not any(
        user.has_rbac_perm(p) for p in importer.permissions
    ):
        raise PermissionDenied()
    return importer


class ImportSerializer(serializers.Serializer):
    file = serializers.FileField()
    dry_run = serializers.BooleanField(default=False)

    def validate_file(self, file):
        if file.size > MAX_UPLOAD_BYTES:
            raise serializers.ValidationError(_("The file is larger than 10 MB."))
        return file


class ImportView(APIView):
    """POST an .xlsx (`file`, `dry_run`) for `developers`, `cards` or `balances`.
    All or nothing: every error is listed with its Excel row; nothing is saved if there is
    one. `dry_run=true` only checks. Permissions: excel.import and developer.create/update,
    card.assign or finance.deposit."""

    permission_classes = [HasPermissions]
    required_permissions = {"post": []}  # per kind, in _importer
    parser_classes = [MultiPartParser]

    @extend_schema(request=ImportSerializer, responses=OpenApiTypes.OBJECT)
    def post(self, request, kind):
        _importer(kind, request.user)
        body = ImportSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        result = run_import(
            kind,
            body.validated_data["file"],
            actor=request.user,
            dry_run=body.validated_data["dry_run"],
        )
        return Response(result)


class TemplateView(APIView):
    """GET an empty import file for `kind`: the header and one example row."""

    permission_classes = [HasPermissions]
    required_permissions = {"get": []}

    @extend_schema(responses={(200, XLSX): OpenApiTypes.BINARY})
    def get(self, request, kind):
        importer = _importer(kind, request.user)
        return _file(template(str(importer.title), importer.columns), f"import-{kind}.xlsx")


EXPORTS = {
    "developers": ("developer.view", False),
    "money": ("finance.view", True),
    "goods": ("good.view", True),
    "finance-stats": ("finance.view", True),
}


class ExportView(APIView):
    """GET `developers`, `money`, `goods` or `finance-stats` (the finance statistics page)
    as .xlsx. All but `developers` take a period (`date_from`, `date_to`; default the last
    30 days); `developers` takes the developer list's filters and search. Limited to the
    user's buildings like the lists."""

    permission_classes = [HasPermissions]
    required_permissions = {"get": []}  # per kind, below

    @extend_schema(parameters=[PeriodSerializer], responses={(200, XLSX): OpenApiTypes.BINARY})
    def get(self, request, kind):
        if kind not in EXPORTS:
            raise NotFound()
        permission, period = EXPORTS[kind]
        if not request.user.has_rbac_perms([permission, "excel.export"]):
            raise PermissionDenied()
        stamp = timezone.localdate().isoformat()
        if not period:
            book = exports.developers_workbook(request.user, request.query_params)
            return _file(book, f"developers-{stamp}.xlsx")
        dates = PeriodSerializer(data=request.query_params)
        dates.is_valid(raise_exception=True)
        first, last = dates.validated_data["date_from"], dates.validated_data["date_to"]
        build = {
            "money": exports.money_workbook,
            "goods": exports.goods_workbook,
            "finance-stats": exports.finance_stats_workbook,
        }[kind]
        return _file(build(request.user, first, last), f"{kind}-{first}-to-{last}.xlsx")
