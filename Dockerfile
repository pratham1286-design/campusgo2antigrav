# Multi-stage production Dockerfile for CampusGo
FROM python:3.14-slim AS base

# Prevent Python from writing .pyc and buffer stdout
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend and frontend source files
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# Initialize database and seed demo data on build
RUN python backend/seed_data.py

# Create non-root user for security
RUN useradd -m -u 1000 campusgo && chown -R campusgo:campusgo /app
USER campusgo

# Expose default application port
EXPOSE 5000

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5000/api/campus/landmarks || exit 1

# Start production server
CMD ["python", "backend/app.py"]
