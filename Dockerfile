FROM python:3.12-slim-bookworm AS dependencies
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --only-binary=:all: -r requirements.txt \
    && /opt/venv/bin/pip check

FROM python:3.12-slim-bookworm AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN groupadd --gid 10001 orderflow \
    && useradd --uid 10001 --gid orderflow --no-create-home --shell /usr/sbin/nologin orderflow
WORKDIR /app
COPY --from=dependencies /opt/venv /opt/venv
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini .
USER orderflow
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=6 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/db', timeout=3).close()"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", "--no-access-log", "--timeout-graceful-shutdown", "25"]
