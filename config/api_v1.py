from django.urls import include, path

from common.system_views import (
    BackupFileView,
    BackupRestoreView,
    BackupRunView,
    BackupUploadView,
    BackupView,
    DataResetView,
    KeptDatabaseView,
    RecordDeleteView,
)

urlpatterns = [
    path("", include("apps.accounts.urls")),
    path("", include("apps.audit.urls")),
    path("", include("apps.developers.urls")),
    path("", include("apps.rfid.urls")),
    path("", include("apps.attendance.urls")),
    path("", include("apps.sellers.urls")),
    path("", include("apps.goods.urls")),
    path("", include("apps.finance.urls")),
    path("", include("apps.purchases.urls")),
    path("", include("apps.bookings.urls")),
    path("", include("apps.realtime.urls")),
    path("", include("apps.stats.urls")),
    path("", include("apps.spreadsheets.urls")),
    path("system/data-reset/", DataResetView.as_view(), name="system-data-reset"),
    path(
        "system/records/<str:kind>/<int:pk>/",
        RecordDeleteView.as_view(),
        name="system-record-delete",
    ),
    path("system/backups/", BackupView.as_view(), name="system-backups"),
    path("system/backups/run/", BackupRunView.as_view(), name="system-backups-run"),
    path("system/backups/files/<str:name>/", BackupFileView.as_view(), name="system-backup-file"),
    path("system/backups/restore/", BackupRestoreView.as_view(), name="system-backups-restore"),
    path("system/backups/upload/", BackupUploadView.as_view(), name="system-backups-upload"),
    path(
        "system/backups/kept/<str:name>/", KeptDatabaseView.as_view(), name="system-kept-database"
    ),
]
