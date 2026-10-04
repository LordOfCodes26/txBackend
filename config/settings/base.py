from datetime import timedelta
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
# Settings come from the process environment, then from DJANGO_ENV_FILE (Windows services,
# which have no EnvironmentFile), then from the project's .env (development).
for _env_file in (env("DJANGO_ENV_FILE", default=""), BASE_DIR / ".env"):
    if _env_file and Path(_env_file).exists():
        environ.Env.read_env(Path(_env_file), overwrite=False)

SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    # Third party
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "django_filters",
    "drf_spectacular",
    "drf_spectacular_sidecar",
    "corsheaders",
    # Local
    "common",
    "apps.accounts",
    "apps.audit",
    "apps.developers",
    "apps.rfid",
    "apps.attendance",
    "apps.sellers",
    "apps.goods",
    "apps.finance",
    "apps.purchases",
    "apps.seller_finance",
    "apps.bookings",
    "apps.realtime",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "common.middleware.RequestContextMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# --- Database ---------------------------------------------------------------
# PostgreSQL is required: the business modules rely on row locks
# (select_for_update), partial unique constraints and DB triggers.
DATABASES = {"default": env.db("DATABASE_URL")}
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Redis / cache / Celery -------------------------------------------------
REDIS_URL = env("REDIS_URL", default="redis://localhost:6379/0")

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {"SOCKET_CONNECT_TIMEOUT": 2, "SOCKET_TIMEOUT": 2},
    }
}

# Realtime (WebSockets via Django Channels, served by uvicorn at /ws/).
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": [REDIS_URL], "capacity": 500, "expiry": 30},
    }
}

CELERY_BROKER_URL = env("CELERY_BROKER_URL", default=REDIS_URL)
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=False)
CELERY_TIMEZONE = "UTC"

# --- Auth -------------------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=env.int("JWT_ACCESS_MINUTES", default=15)),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=env.int("JWT_REFRESH_DAYS", default=7)),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "SIGNING_KEY": env("JWT_SIGNING_KEY", default=SECRET_KEY),
}

# --- DRF --------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PAGINATION_CLASS": "common.pagination.StandardPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "EXCEPTION_HANDLER": "common.exceptions.exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_RATES": {"auth": env("THROTTLE_AUTH_RATE", default="10/min")},
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Backend API",
    "VERSION": "1.0.0",
    "SCHEMA_PATH_PREFIX": r"/api/v1",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    # Door IP authentication can't be expressed in OpenAPI; it's documented in
    # docs/DEVICE_INTEGRATION.md instead.
    "AUTHENTICATION_WHITELIST": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
        "apps.rfid.authentication.DeviceAuthentication",
    ],
    "ENUM_NAME_OVERRIDES": {
        "DeveloperStatusEnum": "apps.developers.models.DeveloperStatus",
        "CardStatusEnum": "apps.rfid.models.CardStatus",
        "AttendanceEventTypeEnum": "apps.attendance.models.EventType",
        "SellerStatusEnum": "apps.sellers.models.SellerStatus",
        "AccountStatusEnum": "apps.finance.models.AccountStatus",
        "PayoutStatusEnum": "apps.seller_finance.models.PayoutStatus",
        "GoodKindEnum": "apps.goods.models.GoodKind",
        "ScanDirectionEnum": "apps.rfid.models.ScanDirection",
        "DeviceDirectionEnum": "apps.rfid.models.DeviceDirection",
    },
    # Serve Swagger UI assets locally: the server may run without internet access.
    "SWAGGER_UI_DIST": "SIDECAR",
    "SWAGGER_UI_FAVICON_HREF": "SIDECAR",
    "REDOC_DIST": "SIDECAR",
}

CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])

# --- I18N / time ------------------------------------------------------------
# Clients pick the language per request with `Accept-Language` (en, ko). Korean texts follow
# DPRK (조선어) usage; any Korean request (ko, ko-KR, ko-KP) gets them.
LANGUAGES = [("en", "English"), ("ko-kp", "조선어")]
LANGUAGE_CODE = env("LANGUAGE_CODE", default="en")  # when the client sends no preference
LOCALE_PATHS = [BASE_DIR / "locale"]
# Door and till readers can't send a language: their screen texts use this one.
DEVICE_LANGUAGE = env("DEVICE_LANGUAGE", default="en")
TIME_ZONE = env("TIME_ZONE", default="UTC")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # Hashed filenames + compression; nginx can still serve STATIC_ROOT directly.
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
MEDIA_URL = "media/"
MEDIA_ROOT = env("MEDIA_ROOT", default=str(BASE_DIR / "media"))

# Only trust X-Forwarded-For when running behind our own reverse proxy.
TRUST_X_FORWARDED_FOR = env.bool("TRUST_X_FORWARDED_FOR", default=False)

# --- RFID -------------------------------------------------------------------
# Repeat scans of the same card within this many seconds are marked DUPLICATE.
RFID_DEBOUNCE_SECONDS = env.int("RFID_DEBOUNCE_SECONDS", default=10)
# Scans timestamped further than this in the future are rejected (reader clock wrong).
RFID_MAX_FUTURE_SKEW_SECONDS = env.int("RFID_MAX_FUTURE_SKEW_SECONDS", default=300)
# A device counts as offline when it hasn't been heard from for this long.
# Devices should send a heartbeat at least every RFID_HEARTBEAT_SECONDS.
RFID_HEARTBEAT_SECONDS = env.int("RFID_HEARTBEAT_SECONDS", default=30)
RFID_DEVICE_OFFLINE_AFTER_SECONDS = env.int("RFID_DEVICE_OFFLINE_AFTER_SECONDS", default=120)
# Maximum scans in one buffered batch upload.
RFID_BATCH_MAX_EVENTS = env.int("RFID_BATCH_MAX_EVENTS", default=500)
# Raw TCP listener for the devices ($ID:...,TYPE:...,UID=...$ frames; manage.py run_rfid_tcp).
RFID_TCP_HOST = env("RFID_TCP_HOST", default="0.0.0.0")
RFID_TCP_PORT = env.int("RFID_TCP_PORT", default=9100)
RFID_TCP_MAX_FRAME_BYTES = env.int("RFID_TCP_MAX_FRAME_BYTES", default=2048)
RFID_TCP_IDLE_TIMEOUT_SECONDS = env.int("RFID_TCP_IDLE_TIMEOUT_SECONDS", default=300)
RFID_TCP_MAX_CONNECTIONS = env.int("RFID_TCP_MAX_CONNECTIONS", default=200)
RFID_TCP_MAX_CONNECTIONS_PER_IP = env.int("RFID_TCP_MAX_CONNECTIONS_PER_IP", default=10)
# Every packet and its answer is kept in the TCP log (admins) for this many days.
RFID_TCP_LOG_DAYS = env.int("RFID_TCP_LOG_DAYS", default=30)

# --- Attendance -------------------------------------------------------------
# How scans become IN/OUT: "none" (first/last scan only), "toggle" (alternate IN/OUT),
# "device" (reader direction). See apps/attendance/rules.py. After changing it or the
# day-start hour, run `manage.py rebuild_attendance`.
ATTENDANCE_DIRECTION_RULE = env("ATTENDANCE_DIRECTION_RULE", default="none")
# Scans before this local hour count for the previous working day (night shifts).
ATTENDANCE_DAY_START_HOUR = env.int("ATTENDANCE_DAY_START_HOUR", default=0)

# --- Sellers & goods ----------------------------------------------------------
# Single currency for all prices and balances (ISO 4217 code shown to clients).
CURRENCY = env("CURRENCY", default="USD")
GOOD_IMAGE_MAX_BYTES = env.int("GOOD_IMAGE_MAX_BYTES", default=5 * 1024 * 1024)
GOOD_MAX_IMAGES = env.int("GOOD_MAX_IMAGES", default=10)

# --- Finance ------------------------------------------------------------------
# Largest single deposit. Bigger amounts will go through approvals (later phase).
FINANCE_MAX_DEPOSIT = env("FINANCE_MAX_DEPOSIT", default="1000.00")
# Developer PIN entered at the till: lock after this many wrong attempts, for this long.
PURCHASE_PIN_MAX_ATTEMPTS = env.int("PURCHASE_PIN_MAX_ATTEMPTS", default=5)
PURCHASE_PIN_LOCKOUT_MINUTES = env.int("PURCHASE_PIN_LOCKOUT_MINUTES", default=15)
# A card tapped on a till reader is valid for confirming the purchase for this long.
PURCHASE_CARD_PRESENTATION_SECONDS = env.int("PURCHASE_CARD_PRESENTATION_SECONDS", default=120)
# Allow the browser to send a typed card UID at checkout. Off: the card must come from the
# counter's TILL device, so a seller cannot charge an arbitrary card number.
PURCHASE_ALLOW_MANUAL_CARD_UID = env.bool("PURCHASE_ALLOW_MANUAL_CARD_UID", default=False)

# --- Backups ------------------------------------------------------------------
# Written by scripts/backup/*.sh, read by GET /health/backup/.
BACKUP_STATUS_FILE = env("BACKUP_STATUS_FILE", default="/var/lib/backend/backup-status.json")
BACKUP_MAX_DUMP_AGE_HOURS = env.int("BACKUP_MAX_DUMP_AGE_HOURS", default=26)
BACKUP_MAX_BASE_AGE_DAYS = env.int("BACKUP_MAX_BASE_AGE_DAYS", default=8)
# Point-in-time recovery (base backups + WAL archiving) is required on Linux; the Windows
# kit makes nightly verified dumps only and turns this off.
BACKUP_REQUIRE_PITR = env.bool("BACKUP_REQUIRE_PITR", default=True)

# --- Test console -------------------------------------------------------------
# A staging-only page (/test-console/) to exercise doors and tills end to end.
# Never enable it in production.
TEST_CONSOLE_ENABLED = env.bool("TEST_CONSOLE_ENABLED", default=False)

# --- Logging ----------------------------------------------------------------
LOG_LEVEL = env("LOG_LEVEL", default="INFO")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"request_context": {"()": "common.logging.RequestContextFilter"}},
    "formatters": {
        "default": {
            "format": "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "default",
            "filters": ["request_context"],
        },
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django.db.backends": {"level": "WARNING"},
    },
}
