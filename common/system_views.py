from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.http import StreamingHttpResponse
from django.utils.translation import gettext_lazy as _
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import record_audit
from common import backups, data_reset, record_delete
from common.exceptions import DomainError
from common.health import backup_health
from common.permissions import HasPermissions


class DataResetSerializer(serializers.Serializer):
    confirm = serializers.CharField(trim_whitespace=False)


class DataResetView(APIView):
    """Delete all data except users, roles, readers and the stores (`system.data_reset`,
    Admin). GET: what would be deleted and kept, and the phrase to type. POST
    `{"confirm": "<phrase>"}`: back up the database, then delete. See common/data_reset.py."""

    permission_classes = [HasPermissions]
    required_permissions = {"get": ["system.data_reset"], "post": ["system.data_reset"]}

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        return Response(data_reset.summary())

    @extend_schema(request=DataResetSerializer, responses=OpenApiTypes.OBJECT)
    def post(self, request):
        body = DataResetSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        result = data_reset.reset_data(actor=request.user, confirm=body.validated_data["confirm"])
        return Response(result)


class BackupSettingsSerializer(serializers.Serializer):
    backup_time = serializers.RegexField(r"^([01]\d|2[0-3]):[0-5]\d$", required=False)
    keep_daily_days = serializers.IntegerField(min_value=1, max_value=365, required=False)
    keep_base_backups = serializers.IntegerField(min_value=1, max_value=52, required=False)
    offsite_dir = serializers.CharField(allow_blank=True, max_length=255, required=False)
    offsite_rsync = serializers.CharField(allow_blank=True, max_length=255, required=False)


class BackupView(APIView):
    """The Backups page (`system.backup`, Admin). GET: health (as /health/backup/),
    settings, last and next run, backup files and the last run's log. PATCH: change the
    settings (backup time, how long to keep, off-site target)."""

    permission_classes = [HasPermissions]
    required_permissions = {"get": ["system.backup"], "patch": ["system.backup"]}

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        return Response({"health": backup_health(), **backups.backup_status()})

    @extend_schema(request=BackupSettingsSerializer, responses=OpenApiTypes.OBJECT)
    def patch(self, request):
        body = BackupSettingsSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        values = {k: str(v).strip() for k, v in body.validated_data.items()}
        before = backups.backup_status()["config"]
        with transaction.atomic():
            backups.change_settings(values)
            record_audit(
                "system.backup_settings_changed",
                actor=request.user,
                entity_type="system",
                entity_id="backups",
                old_values={k: before.get(k) for k in values},
                new_values=values,
            )
        return Response({"health": backup_health(), **backups.backup_status()})


class BackupRunView(APIView):
    """POST: start the nightly backup now (dump, restore check, off-site copy). It runs in
    the background; GET the Backups page to see when it has finished."""

    permission_classes = [HasPermissions]
    required_permissions = {"post": ["system.backup"]}

    @extend_schema(request=None, responses=OpenApiTypes.OBJECT)
    def post(self, request):
        result = backups.start_backup()
        record_audit(
            "system.backup_started", actor=request.user, entity_type="system", entity_id="backups"
        )
        return Response(result, status=202)


class BackupFileView(APIView):
    """GET one nightly dump (`system.backup`). It holds personal data and PIN hashes."""

    permission_classes = [HasPermissions]
    required_permissions = {"get": ["system.backup"]}

    @extend_schema(responses={(200, "application/octet-stream"): OpenApiTypes.BINARY})
    def get(self, request, name):
        known = {f["name"]: f for f in backups.backup_status()["dumps"]}
        if name not in known:
            raise NotFound()
        chunks = backups.open_dump(name)
        record_audit(
            "system.backup_downloaded",
            actor=request.user,
            entity_type="system",
            entity_id="backups",
            new_values={"file": name},
        )

        response = StreamingHttpResponse(chunks, content_type="application/octet-stream")
        response["Content-Disposition"] = f'attachment; filename="{name}"'
        response["Content-Length"] = str(known[name]["bytes"])
        return response


RESTORE_PHRASE = "RESTORE"


class RestoreNotConfirmed(DomainError):
    code = "RESTORE_NOT_CONFIRMED"
    default_detail = _("Type RESTORE to confirm.")


class BackupRestoreSerializer(serializers.Serializer):
    file = serializers.CharField(max_length=100)
    confirm = serializers.CharField(trim_whitespace=False)


class BackupRestoreView(APIView):
    """POST `{file, confirm: "RESTORE"}`: put that backup back as the live data
    (`system.backup`). It runs as its own job on the server: first a fresh backup of the
    current data, then a check of the file, then the restore; the current database is kept
    under a new name. The web app is offline for a minute or two meanwhile; the outcome
    shows on the Backups page (`restore`) when it is back."""

    permission_classes = [HasPermissions]
    required_permissions = {"post": ["system.backup"]}

    @extend_schema(request=BackupRestoreSerializer, responses=OpenApiTypes.OBJECT)
    def post(self, request):
        body = BackupRestoreSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        if body.validated_data["confirm"] != RESTORE_PHRASE:
            raise RestoreNotConfirmed()
        name = body.validated_data["file"]
        if name not in {f["name"] for f in backups.backup_status()["dumps"]}:
            raise NotFound()
        # Recorded in the current database, which the restore keeps under a new name.
        record_audit(
            "system.restore_started",
            actor=request.user,
            entity_type="system",
            entity_id="backups",
            new_values={"file": name},
        )
        return Response(backups.start_restore(name, request.user.username), status=202)


class BackupUploadSerializer(serializers.Serializer):
    file = serializers.FileField()


class BackupUploadView(APIView):
    """POST a .dump (multipart `file`, `system.backup`): saved next to the nightly backups
    as uploaded-<stamp>.dump, if pg_restore can read it. Then it can be restored."""

    permission_classes = [HasPermissions]
    required_permissions = {"post": ["system.backup"]}
    parser_classes = [MultiPartParser]

    @extend_schema(request=BackupUploadSerializer, responses=OpenApiTypes.OBJECT)
    def post(self, request):
        body = BackupUploadSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        upload = body.validated_data["file"]
        result = backups.upload_dump(upload)
        record_audit(
            "system.backup_uploaded",
            actor=request.user,
            entity_type="system",
            entity_id="backups",
            new_values={"file": result["name"], "original_name": upload.name[:100]},
        )
        return Response(result, status=201)


class KeptDatabaseView(APIView):
    """DELETE a database kept by an earlier restore (`system.backup`), once the restored
    data is fine: it is the way back, so this can't be undone."""

    permission_classes = [HasPermissions]
    required_permissions = {"delete": ["system.backup"]}

    @extend_schema(responses={204: None})
    def delete(self, request, name):
        backups.drop_kept_database(name)
        record_audit(
            "system.kept_database_deleted",
            actor=request.user,
            entity_type="system",
            entity_id="backups",
            new_values={"database": name},
        )
        return Response(status=204)


class RecordDeleteSerializer(serializers.Serializer):
    confirm = serializers.CharField(trim_whitespace=False)


class RecordDeleteView(APIView):
    """Delete a record for good, with its history (`system.delete_records`, Admin).
    `kind`: user, developer, card, reader, seller, position, good.
    GET: what would be deleted and what stays (counts). POST `{"confirm": "DELETE"}`: back
    up the database, then delete. See common/record_delete.py for what goes with each."""

    permission_classes = [HasPermissions]
    required_permissions = {
        "get": ["system.delete_records"],
        "post": ["system.delete_records"],
    }

    def _check(self, kind):
        if kind not in record_delete.KINDS:
            raise NotFound()

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request, kind, pk):
        self._check(kind)
        try:
            plan = record_delete.plan(kind, pk, request.user)
        except ObjectDoesNotExist as exc:
            raise NotFound() from exc
        return Response(
            {
                "kind": kind,
                "label": plan.label,
                "deletes": plan.deletes,
                "keeps": plan.keeps,
                "phrase": record_delete.CONFIRM_PHRASE,
            }
        )

    @extend_schema(request=RecordDeleteSerializer, responses=OpenApiTypes.OBJECT)
    def post(self, request, kind, pk):
        self._check(kind)
        body = RecordDeleteSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            result = record_delete.delete_record(
                kind, pk, actor=request.user, confirm=body.validated_data["confirm"]
            )
        except ObjectDoesNotExist as exc:
            raise NotFound() from exc
        return Response(result)
