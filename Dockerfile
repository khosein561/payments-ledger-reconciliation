FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LEDGER_MIGRATIONS_DIR=/app/db/migrations

WORKDIR /app

# Install dependencies first so Docker caches this layer when only your code changes.
COPY pyproject.toml ./
COPY src ./src
COPY db ./db
RUN pip install --no-cache-dir .

# Run as an unprivileged user: a compromised web process should not be root.
RUN useradd --create-home appuser
USER appuser

EXPOSE 8000
CMD ["uvicorn", "ledger.ingest.webhook_app:app", "--host", "0.0.0.0", "--port", "8000"]
