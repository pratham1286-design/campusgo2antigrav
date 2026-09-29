FROM python:3.12-slim

# Prevent Python from writing .pyc and buffer stdout
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
# The database lives in its own directory so a volume can persist it without
# also freezing the application code (mounting over /app/backend did that).
ENV DATABASE_PATH=/app/data/campusgo.db

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

# Non-root user owns only the data directory.
RUN useradd -m -u 1000 campusgo && mkdir -p /app/data && chown -R campusgo:campusgo /app/data
USER campusgo

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:5000/api/config || exit 1

# SECRET_KEY must be set (the app refuses to start with a known placeholder).
# Demo personas are created only when SEED_DEMO_DATA=1, and only into an empty
# database, so restarts never wipe real data.
CMD ["sh", "-c", "if [ \"$SEED_DEMO_DATA\" = \"1\" ]; then python backend/seed_data.py --if-empty; fi && exec gunicorn --chdir backend --workers 1 --threads 4 --bind 0.0.0.0:5000 app:app"]
