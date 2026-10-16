
FROM python:3.12.8-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies before copying application source.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create and use a non-root user.
RUN groupadd --system appgroup && \
    useradd --system --gid appgroup --home-dir /app appuser

COPY --chown=appuser:appgroup app/ ./app/
COPY --chown=appuser:appgroup migrations/ ./migrations/

USER appuser

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
