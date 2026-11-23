import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
SITE_NAME = "Absolute Cinema"
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    raise ImproperlyConfigured(
        "Set DJANGO_SECRET_KEY in the environment. For local Windows development, run .\\start-local.ps1."
    )

DEBUG = os.environ.get("DJANGO_DEBUG") == "1"
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "catalog",
    "billing",
    "operations",
    "viewing",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "accounts.middleware.ViewerDeviceMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "config.context_processors.site_brand",
            ],
        },
    },
]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": os.environ.get("DB_HOST", "127.0.0.1"),
        "PORT": os.environ.get("DB_PORT", "5433"),
        "NAME": os.environ.get("DB_NAME", "online_cinema"),
        "USER": os.environ.get("DB_USER", "postgres"),
        "PASSWORD": os.environ.get("DB_PASSWORD", ""),
        "OPTIONS": {"connect_timeout": 5},
    },
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = ["accounts.backends.AccountBackend"]
LOGIN_REDIRECT_URL = "admin:index"
LOGIN_URL = "viewer-login"
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
PASSWORD_RESET_TIMEOUT = 1800
EMAIL_BACKEND = "django.core.mail.backends.filebased.EmailBackend"
EMAIL_FILE_PATH = os.environ.get("CINEMA_MAIL_DIR", str(BASE_DIR / "private" / "mail"))
DEFAULT_FROM_EMAIL = "no-reply@absolute-cinema.test"
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"reset_url": {"()": "accounts.logging.RedactResetURL"}},
    "loggers": {
        "django.server": {"filters": ["reset_url"]},
        "django.request": {"filters": ["reset_url"]},
    },
}
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
        "OPTIONS": {"user_attributes": ["login", "email", "full_name"]},
    },
    {"NAME": "accounts.validators.AccountPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LANGUAGE_CODE = "ru"
TIME_ZONE = "Europe/Moscow"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "collected-static"
CATALOG_MEDIA_ROOT = Path(os.environ.get("CATALOG_MEDIA_ROOT", BASE_DIR / "private" / "catalog"))
BACKUP_ROOT = Path(os.environ.get("CINEMA_BACKUP_DIR", BASE_DIR / "private" / "backups"))
POSTGRES_BIN_DIR = os.environ.get("POSTGRES_BIN_DIR", "")
LOCAL_FFPROBE = next(iter(sorted((BASE_DIR / ".venv" / "ffmpeg-tools").glob("*/bin/ffprobe.exe"))), None)
FFPROBE_BIN = os.environ.get("FFPROBE_BIN") or (str(LOCAL_FFPROBE) if DEBUG and LOCAL_FFPROBE else "ffprobe")
MEDIA_PROBE_ENABLED = DEBUG and os.environ.get("DJANGO_MEDIA_PROBE") == "1"
MEDIA_PROBE_FILE = os.environ.get("DJANGO_MEDIA_PROBE_FILE", "")
