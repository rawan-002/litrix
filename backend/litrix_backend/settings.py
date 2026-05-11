"""
Django settings for Litrix Backend.

Architecture notes:
    • The backend READS from the existing LitrixDB (populated by the
      scraper). It does NOT manage Django-style migrations on the
      domain tables (Users, Researcher, ResearchPaper, etc.).
    • Django's auth + admin tables go to a DIFFERENT schema-prefix to
      keep the domain DB clean. We use the public schema for both, but
      Django's tables are prefixed (django_*, auth_*, etc.) so they
      don't conflict.
    • All domain models declare `managed = False` so Django won't try
      to ALTER them.
    • Production deployment expects DATABASE_URL env var (set by Render,
      Railway, Heroku, etc.). Falls back to discrete DB_* vars locally.
"""
from pathlib import Path
import os
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR.parent / '.env')

# ----------------------------------------------------------------------
# Security-critical defaults.
#
# Why fail-fast in production?
#   A missing DJANGO_SECRET_KEY in production used to silently fall back
#   to a hardcoded dev string — which means every Django session token,
#   password reset token, and signed cookie becomes forgeable by anyone
#   with the source. Same story for DEBUG: a missing env var would boot
#   the server with `DEBUG = True`, leaking stack traces with secrets.
#
# New behavior:
#   • DEBUG defaults to FALSE (safe default).
#   • SECRET_KEY: if DEBUG is True, a dev-only key is allowed (so local
#     setup keeps working). If DEBUG is False (production), a missing
#     SECRET_KEY raises ImproperlyConfigured — the deploy hard-stops
#     instead of running insecurely.
# ----------------------------------------------------------------------
from django.core.exceptions import ImproperlyConfigured

DEBUG = os.getenv('DJANGO_DEBUG', 'false').lower() == 'true'

SECRET_KEY = os.getenv('DJANGO_SECRET_KEY')
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = 'dev-only-change-me-in-production-please-and-thank-you'
    else:
        raise ImproperlyConfigured(
            'DJANGO_SECRET_KEY env var is required in production '
            '(DEBUG=False). Generate one with: '
            'python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"'
        )

ALLOWED_HOSTS = os.getenv(
    'DJANGO_ALLOWED_HOSTS',
    'localhost,127.0.0.1'
).split(',')

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')


INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'corsheaders',
    'django_filters',

    'accounts',
    'analytics',
]

AUTH_USER_MODEL = 'accounts.User'

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'litrix_backend.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'litrix_backend.wsgi.application'


_db_url = os.getenv('DATABASE_URL')
if _db_url:
    import dj_database_url
    DATABASES = {
        'default': dj_database_url.parse(
            _db_url,
            conn_max_age=600,
            ssl_require=not DEBUG,
        ),
    }
else:
    DATABASES = {
        'default': {
            'ENGINE':   'django.db.backends.postgresql',
            'NAME':     os.getenv('DB_NAME', 'LitrixDB'),
            'USER':     os.getenv('DB_USER', 'postgres'),
            'PASSWORD': os.getenv('DB_PASSWORD', ''),
            'HOST':     os.getenv('DB_HOST', 'localhost'),
            'PORT':     os.getenv('DB_PORT', '5432'),
        }
    }


AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Riyadh'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# ----------------------------------------------------------------------
# DRF Renderers — why DEBUG-conditional?
#   The BrowsableAPIRenderer renders the HTML "explore the API" pages
#   that make local dev pleasant. In production it's pure attack surface:
#   it exposes endpoint schemas, available methods, and a CSRF-bypassing
#   POST form to anonymous users who hit a 401 URL. Keep it for dev only.
#
# DRF Throttling — why?
#   The auth endpoints (login, register, password-reset) are AllowAny.
#   Without throttling, an attacker can run credential stuffing or
#   email enumeration at full speed. We attach two named scopes:
#     • 'auth_anon' — 5/min for anonymous auth attempts
#     • 'auth_user' — 60/min for authenticated mutations
#   Views opt in via @throttle_classes(...) so the rest of the API
#   (analytics reads, etc.) stays untouched.
# ----------------------------------------------------------------------
_renderers = ['rest_framework.renderers.JSONRenderer']
if DEBUG:
    _renderers.append('rest_framework.renderers.BrowsableAPIRenderer')

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_PAGINATION_CLASS':
        'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 25,
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.OrderingFilter',
        'rest_framework.filters.SearchFilter',
    ],
    'DEFAULT_RENDERER_CLASSES': _renderers,
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.ScopedRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'auth_anon':  '5/min',
        'auth_user': '60/min',
    },
}

from datetime import timedelta
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME':  timedelta(minutes=60),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS':  True,
    'BLACKLIST_AFTER_ROTATION': True,
    'AUTH_HEADER_TYPES': ('Bearer',),
    'USER_ID_FIELD': 'user_id',
    'USER_ID_CLAIM': 'user_id',
}


_extra_origins = os.getenv('CORS_ALLOWED_ORIGINS', '').strip()
CORS_ALLOWED_ORIGINS = [
    'http://localhost:4200',
    'http://127.0.0.1:4200',
]
if _extra_origins:
    CORS_ALLOWED_ORIGINS += [o.strip() for o in _extra_origins.split(',') if o.strip()]

# Regex patterns let us allow ALL Vercel preview deployments
# (litrix-XXX-rawan-002s-projects.vercel.app) without listing each
# explicitly. Set CORS_ALLOWED_ORIGIN_REGEXES env var to override.
import re as _re
_default_regexes = [
    r'^https://litrix(-[a-z0-9]+)*-rawan-002s-projects\.vercel\.app$',
    r'^https://litrix\.vercel\.app$',
]
_regex_env = os.getenv('CORS_ALLOWED_ORIGIN_REGEXES', '').strip()
if _regex_env:
    CORS_ALLOWED_ORIGIN_REGEXES = [r.strip() for r in _regex_env.split(',') if r.strip()]
else:
    CORS_ALLOWED_ORIGIN_REGEXES = _default_regexes

CORS_ALLOW_CREDENTIALS = True
