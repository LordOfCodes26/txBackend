import redis
from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def health(request):
    return JsonResponse({"status": "ok"})


@require_GET
def health_db(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return JsonResponse({"status": "error", "component": "db"}, status=503)
    return JsonResponse({"status": "ok", "component": "db"})


@require_GET
def health_redis(request):
    try:
        client = redis.Redis.from_url(
            settings.REDIS_URL, socket_connect_timeout=2, socket_timeout=2
        )
        client.ping()
    except Exception:
        return JsonResponse({"status": "error", "component": "redis"}, status=503)
    return JsonResponse({"status": "ok", "component": "redis"})
