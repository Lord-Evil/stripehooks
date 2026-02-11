# StripeHooks - Stripe webhook handler with admin UI
# Run with: docker run -p 8000:8000 -v stripehooks_data:/app/data stripehooks
# Persist DB: mount a volume at /app/data (STRIPEHOOKS_DB_PATH defaults to /app/data/stripehooks.db)

FROM python:3.13-alpine

# Build deps for Python packages that need compilation (uvloop, etc.)
RUN apk add --no-cache \
    gcc \
    musl-dev \
    libffi-dev \
    python3-dev

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Remove build deps to keep image smaller
RUN apk del gcc musl-dev libffi-dev python3-dev

COPY . .

# Run as non-root
RUN adduser -D -u 1000 appuser && chown -R appuser:appuser /app
RUN mkdir -p /app/data && chown appuser:appuser /app/data
USER appuser

ENV STRIPEHOOKS_DB_PATH=/app/data/stripehooks.db

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
