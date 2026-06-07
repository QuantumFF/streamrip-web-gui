FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user
RUN groupadd -g 1000 appuser && \
    useradd -r -u 1000 -g appuser appuser

# Set working directory
WORKDIR /app

# Install Python dependencies. streamrip is installed from the upstream `dev`
# branch (not the PyPI release, which lags behind): PyPI's latest is 2.1.0
# while dev/host run 2.2.0+, and the config schema version is tied to the
# streamrip version — a mismatch makes host and container fight over the shared
# self-mounted config.toml (each rewrites it to its own schema on every run).
# Tracking dev keeps the container aligned with the host build.
# Note: `@dev` is a moving target, so rebuilds are not reproducible; Docker
# layer caching means this only re-pulls when this layer is invalidated
# (e.g. `--no-cache` or an earlier-layer change).
RUN pip install --no-cache-dir \
    flask \
    flask-cors \
    "git+https://github.com/nathom/streamrip.git@dev" \
    gunicorn \
    gevent

# Copy application files
COPY app.py /app/
COPY templates /app/templates/
COPY static /app/static/

# Create necessary directories with proper ownership
RUN mkdir -p /downloads /logs /config/streamrip && \
    chown -R 1000:1000 /downloads /logs /config

# Switch to non-root user
USER 1000:1000

# Expose port
EXPOSE 5000

# Run with aggressive worker recycling
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--worker-class", "gevent", "--workers", "2", "--timeout", "60", "app:app"]
