# =============================================================================
# Litrix Backend — Production Dockerfile for Google Cloud Run (Dammam)
# =============================================================================
#
# Why multi-stage build?
#   • Stage 1 (builder): installs build deps (gcc, libpq-dev) needed to compile
#     psycopg2 wheels. These are 200MB+ of toolchain we don't need at runtime.
#   • Stage 2 (runtime): minimal slim image with only Python + compiled wheels.
#   Result: final image ~180MB instead of ~600MB → faster cold starts on Cloud Run.
#
# Why python:3.12-slim?
#   Matches runtime.txt (python-3.12.3). Slim variant excludes docs/man pages.
#
# Why non-root user?
#   Cloud Run runs containers in gVisor sandbox, but defense-in-depth: a process
#   running as UID 1000 can't escalate even if Django gets compromised.
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: Builder
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# psycopg2-binary needs libpq + gcc to compile if wheel unavailable.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip wheel --wheel-dir=/wheels -r requirements.txt


# -----------------------------------------------------------------------------
# Stage 2: Runtime
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_DEBUG=false \
    PORT=8080

# libpq5 is the runtime library (without the -dev headers).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 litrix \
    && useradd --uid 1000 --gid litrix --shell /bin/bash --create-home litrix

WORKDIR /app

# Install pre-built wheels from the builder stage.
COPY --from=builder /wheels /wheels
RUN pip install --no-index --find-links=/wheels /wheels/*.whl \
    && rm -rf /wheels

# Copy the Django project.
COPY --chown=litrix:litrix . .

# Collect static at build time (Whitenoise serves them).
# DJANGO_SECRET_KEY is required by settings.py even for collectstatic,
# so we pass a dummy one — production gets the real key from env vars.
RUN DJANGO_SECRET_KEY=build-time-dummy-key-not-used-at-runtime \
    DJANGO_DEBUG=false \
    python manage.py collectstatic --no-input --clear

# Switch to non-root user.
USER litrix

# Cloud Run injects $PORT. Bind to 0.0.0.0 so the proxy can reach us.
# --workers 2: balanced for Cloud Run's 1 vCPU default (one per CPU + headroom).
# --timeout 120: matches Cloud Run's default request timeout.
# --access-logfile -: send access logs to stdout (Cloud Logging captures them).
EXPOSE 8080
CMD exec gunicorn litrix_backend.wsgi:application \
    --bind 0.0.0.0:${PORT} \
    --workers 2 \
    --threads 4 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    --log-level info
