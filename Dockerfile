FROM python:3.10-alpine

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apk add --no-cache \
    gcc \
    musl-dev \
    ffmpeg \
    linux-headers \
    su-exec

# Unprivileged runtime user (entrypoint fixes ownership of the data volume, then drops to it)
RUN addgroup -S app && adduser -S -G app app

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY app.py .
COPY media.py .
COPY templates/ templates/
COPY static/ static/
COPY entrypoint.sh /entrypoint.sh

# Copy default JSON files for first-run seeding
COPY defaults/ /defaults/

# Make entrypoint executable
RUN chmod +x /entrypoint.sh

# All mutable state (JSON data, jobs.json, secret key, temp dirs) lives in the volume
ENV DATA_DIR=/app/data

# Expose port
EXPOSE 5050

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD wget -qO- "http://127.0.0.1:${PORT:-5050}/login" > /dev/null || exit 1

# Use entrypoint for data setup
ENTRYPOINT ["/entrypoint.sh"]

# Use gunicorn in production, dev server otherwise.
# HTTPS is env-driven: if HTTPS=1, SSL_CERT_FILE and SSL_KEY_FILE point at a
# cert/key pair mounted into the container; gunicorn serves TLS on PORT
# (default 8443) — the certfile/keyfile flags are what enable TLS, the bind
# stays a plain host:port. Plain HTTP otherwise (PORT default 5050).
CMD ["sh", "-c", "\
if [ \"$FLASK_ENV\" = \"production\" ]; then \
    if [ \"$HTTPS\" = \"1\" ]; then \
        exec gunicorn --bind 0.0.0.0:${PORT:-8443} --certfile \"$SSL_CERT_FILE\" --keyfile \"$SSL_KEY_FILE\" \
             --workers 2 --threads 4 --timeout 120 --access-logfile - 'app:app'; \
    else \
        exec gunicorn --bind 0.0.0.0:${PORT:-5050} \
             --workers 2 --threads 4 --timeout 120 --access-logfile - 'app:app'; \
    fi; \
else \
    exec python app.py; \
fi"]
