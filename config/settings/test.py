import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-secret-key-that-is-long-enough-for-hs256")
os.environ.setdefault("DATABASE_URL", "postgres://backend:backend@localhost:5432/backend")

from .base import *  # noqa: E402, F403
from .base import REST_FRAMEWORK  # noqa: E402

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
# Independent of whatever a developer's .env says; tests opt into other values.
ATTENDANCE_DIRECTION_RULE = "none"
TEST_CONSOLE_ENABLED = False
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] = {"auth": "1000/min"}
# Tests don't run collectstatic, so skip the manifest lookup.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
MIDDLEWARE = [m for m in MIDDLEWARE if m != "whitenoise.middleware.WhiteNoiseMiddleware"]  # noqa: F405
