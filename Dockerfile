FROM python:3.13-slim

# Prevent Python from writing .pyc and buffer stdout
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
# Without DATABASE_URL the app uses SQLite here; the directory can be a volume.
ENV DATABASE_PATH=/app/data/campusgo.db

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# .dockerignore keeps tests, local databases and secrets out of the image.
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# Non-root user owns only the data directory.
RUN useradd -m -u 1000 campusgo && mkdir -p /app/data && chown -R campusgo:campusgo /app/data
USER campusgo

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fs http://localhost:5000/healthz || exit 1

# SECRET_KEY must be set: gunicorn.conf.py refuses to start without it. The schema is created on
# startup. Worker count: see backend/gunicorn.conf.py (several workers need DATABASE_URL + REDIS_URL).
CMD ["gunicorn", "--config", "backend/gunicorn.conf.py"]
