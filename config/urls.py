from django.conf import settings
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from common import health

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", health.health, name="health"),
    path("health/db/", health.health_db, name="health-db"),
    path("health/redis/", health.health_redis, name="health-redis"),
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
