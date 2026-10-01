from django.conf import settings
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from common import health, test_console

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", health.health, name="health"),
    path("health/db/", health.health_db, name="health-db"),
    path("health/redis/", health.health_redis, name="health-redis"),
    path("health/backup/", health.health_backup, name="health-backup"),
    path("api/v1/", include("config.api_v1")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
]

if settings.DEBUG:
    from django.conf.urls.static import static

    # Log in/out links for DRF's browsable API, and uploaded files (dev only;
    # nginx serves /media/ in staging and production).
    urlpatterns.append(path("api-auth/", include("rest_framework.urls")))
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

# Staging-only test console; the views answer 404 unless TEST_CONSOLE_ENABLED is set.
urlpatterns += [
    path("test-console/", test_console.console_page, name="test-console"),
    path(
        "api/v1/test-console/door-scan/",
        test_console.SimulatedDoorScanView.as_view(),
        name="test-console-door-scan",
    ),
    path(
        "api/v1/test-console/simulate-tap/",
        test_console.SimulateTapView.as_view(),
        name="test-console-simulate-tap",
    ),
    path(
        "api/v1/test-console/cards/",
        test_console.TestCardsView.as_view(),
        name="test-console-cards",
    ),
]
