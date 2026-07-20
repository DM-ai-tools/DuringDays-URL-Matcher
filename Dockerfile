# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8787 \
    FORWARDED_ALLOW_IPS=*

WORKDIR /app

# curl_cffi needs CA certs; curl for healthchecks
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application package (includes static assets + url_bulk) and root modules
COPY app ./app
COPY config.py matcher.py product_query.py run.py serve.py sitemaps.py url_name.py verify.py kogan_wayback.py ./

RUN mkdir -p \
    cache/custom \
    data \
    output \
    uploads \
    outputs/working \
    outputs/jobs \
    && find . -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true

EXPOSE 8787

# Railway also uses railway.json healthcheckPath; this helps local Docker
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

# Reads PORT / HOST / FORWARDED_ALLOW_IPS from the environment (Railway-safe)
CMD ["python", "serve.py"]
