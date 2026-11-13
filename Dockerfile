# =============================================================================
# Base stage: official minimal Alpine Python runtime
# =============================================================================
FROM python:3.13-alpine AS base

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# =============================================================================
# Builder stage: build wheels / dependencies
# =============================================================================
FROM base AS builder

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir --prefix=/install -r requirements.txt

# =============================================================================
# Development / Test stage: includes dev tooling from pyproject.toml
# =============================================================================
FROM base AS development

# Copy installed production packages
COPY --from=builder /install /usr/local

# Copy dev dependencies and install test/lint tools
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir black flake8 pytest pytest-asyncio pytest-cov

# Copy application source and tests
COPY main.py sanitize.py ./
COPY telegram_mcp ./telegram_mcp
COPY tests ./tests

CMD ["pytest", "--cov", "--cov-report=term-missing"]

# =============================================================================
# Production stage: slim runtime container with non-root appuser
# =============================================================================
FROM base AS production

# Copy only installed dependencies from builder
COPY --from=builder /install /usr/local

# Copy application source code
COPY main.py sanitize.py session_string_generator.py migrate_session.py ./
COPY telegram_mcp ./telegram_mcp

# Create non-root user and setup directories
RUN adduser --disabled-password --gecos "" appuser && \
    mkdir -p /app/data/transcripts /app/data/archive_media && \
    chown -R appuser:appuser /app

USER appuser

# Runtime environment variables (to be supplied at run/container start).
# This fork defaults to a hard read-only client. TELEGRAM_SESSION_NAME points
# at the data volume so the file session and the archive survive restarts.
ENV TELEGRAM_API_ID="" \
    TELEGRAM_API_HASH="" \
    TELEGRAM_SESSION_NAME="/app/data/telegram_session" \
    TELEGRAM_SESSION_STRING="" \
    TELEGRAM_HARD_READONLY="1" \
    TELEGRAM_DATA_DIR="/app/data" \
    TELEGRAM_ARCHIVE_MEDIA="photos" \
    TELEGRAM_TRANSCRIPT_CACHE_DIR="/app/data/transcripts"

CMD ["python", "main.py"]
