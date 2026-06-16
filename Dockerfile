# Stage 1: Builder
FROM python:3.11-slim AS builder

WORKDIR /build

# Copy pyproject.toml and install dependencies
COPY pyproject.toml .
RUN pip install --user --no-cache-dir --upgrade pip && \
    pip install --user --no-cache-dir \
    fastapi>=0.111.0 \
    uvicorn[standard]>=0.29.0 \
    httpx>=0.27.0 \
    pyyaml>=6.0

# Stage 2: Runtime
FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    rsync \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -m -u 1000 appuser

# Copy installed packages from builder
COPY --from=builder /root/.local /home/appuser/.local

# Copy application code
COPY --chown=appuser:appuser agent ./agent
COPY --chown=appuser:appuser config ./config
COPY --chown=appuser:appuser context ./context
COPY --chown=appuser:appuser pyproject.toml .

# Ensure /app and all subdirectories are writable by appuser (for database, logs, etc.)
RUN chmod 755 /app && \
    mkdir -p /app/logs /app/.squid-data && \
    chown -R appuser:appuser /app && \
    chmod -R u+w /app

# Set environment variables
ENV PATH=/home/appuser/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SQUID_DB_PATH=/app/.squid-data/squid.db

# Switch to non-root user
USER appuser

# Expose port
EXPOSE 8000

# Health check — simple TCP check on the port
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import socket; socket.create_connection(('localhost', 8000), timeout=1)"

# Run the app
CMD ["uvicorn", "agent.server:app", "--host", "0.0.0.0", "--port", "8000"]
