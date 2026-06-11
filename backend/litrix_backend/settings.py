"""
Django settings for the Litrix backend.

The backend reads from the scraper-populated LitrixDB and never runs
migrations on the domain tables (they're all managed = False); Django's own
auth/admin tables share the schema but are prefixed (django_*, auth_*) so
they don't collide. Production reads DATABASE_URL; locally it falls back to
discrete DB_* vars.
"""
from pathlib import Path
import os
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR.parent / '.env')

# Security-critical defaults. DEBUG defaults to False so a missing env var
# can't boot the server leaking stack traces. A dev-only SECRET_KEY is only
# allowed when DEBUG is True; in production a missing key hard-stops the
# deploy rather than falling back to a hardcoded string that would make every
# session token, reset token, and signed cookie forgeable from the source.
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


# BrowsableAPIRenderer is dev-only: handy locally, but in production it's
# attack surface — it exposes endpoint schemas and a CSRF-bypassing POST form
# to anonymous users. Throttling guards the AllowAny auth endpoints (login,
# register, reset) against credential stuffing and email enumeration:
# auth_anon 5/min, auth_user 60/min. Views opt in via @throttle_classes so
# the rest of the API stays untouched.
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

# Regex patterns allow every Vercel preview deployment
# (litrix-XXX-rawan-002s-projects.vercel.app) without listing each one.
# Override with the CORS_ALLOWED_ORIGIN_REGEXES env var.
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
