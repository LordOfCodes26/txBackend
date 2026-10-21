from django.urls import path
from rest_framework.routers import SimpleRouter

from . import views

router = SimpleRouter()
router.register("purchases", views.PurchaseViewSet, basename="purchase")

urlpatterns = [
    path(
        "preferential/settings/",
        views.PreferentialSettingsView.as_view(),
        name="preferential-settings",
    ),
    path(
        "preferential/report/", views.PreferentialReportView.as_view(), name="preferential-report"
    ),
    path(
        "preferential/report/export/",
        views.PreferentialReportView.as_view(),
        {"export": True},
        name="preferential-report-export",
    ),
    *router.urls,
]
