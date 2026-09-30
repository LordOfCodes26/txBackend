"""Production code paths served over plain HTTP, for reaching a test server directly.

Differences from prod: no HTTPS redirect/HSTS, cookies allowed over HTTP (otherwise the
admin login cannot work), and the API docs are public. Everything else — DEBUG off,
JSON-only errors, password validation — matches production.

Do not use this for real data: logins and tokens travel unencrypted.
"""

from .prod import *  # noqa: F403
from .prod import SPECTACULAR_SETTINGS, env

SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

SPECTACULAR_SETTINGS["SERVE_PERMISSIONS"] = ["rest_framework.permissions.AllowAny"]
