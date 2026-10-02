from .base import *  # noqa: F403
from .base import SPECTACULAR_SETTINGS, env

DEBUG = False

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=60 * 60 * 24 * 30)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# Health checks are served over plain HTTP inside the cluster.
SECURE_REDIRECT_EXEMPT = [r"^health/"]

SPECTACULAR_SETTINGS["SERVE_PERMISSIONS"] = ["rest_framework.permissions.IsAuthenticated"]
# The docs page is opened in a browser: accept the Django admin login (session) there, as
# well as API tokens. Without it the docs answered 401 to everyone.
SPECTACULAR_SETTINGS["SERVE_AUTHENTICATION"] = [
    "rest_framework.authentication.SessionAuthentication",
    "rest_framework_simplejwt.authentication.JWTAuthentication",
]

# HSTS preload is a public browser list; irrelevant for an offline/internal server.
SILENCED_SYSTEM_CHECKS = ["security.W021"]
