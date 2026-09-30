from django.urls import include, path

urlpatterns = [
    path("", include("apps.accounts.urls")),
    path("", include("apps.audit.urls")),
    path("", include("apps.developers.urls")),
    path("", include("apps.rfid.urls")),
    path("", include("apps.attendance.urls")),
]
