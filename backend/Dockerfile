# Build context is the repo root:  docker build -f backend/Dockerfile .
# ---------- build stage: wheels only, keeps the runtime image slim ----------
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1
WORKDIR /wheels
RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ libpq-dev \
    && rm -rf /var/lib/apt/lists/*
COPY backend/requirements.txt .
RUN pip wheel -r requirements.txt -w /wheels

# ---------- runtime ----------
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 MPLBACKEND=Agg \
    PIP_NO_CACHE_DIR=1 DATA_DIR=/data
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin akalonics
COPY --from=builder /wheels /wheels
COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-index --find-links=/wheels -r /tmp/requirements.txt && rm -rf /wheels
WORKDIR /app
COPY --chown=akalonics:akalonics backend /app/backend
COPY --chown=akalonics:akalonics samples /app/samples
RUN mkdir -p /data && chown -R akalonics:akalonics /data /app
USER 10001
WORKDIR /app/backend
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
