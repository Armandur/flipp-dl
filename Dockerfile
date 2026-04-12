FROM python:3.12-slim

# Non-root user for security
RUN groupadd -r flipp && useradd -r -g flipp -d /app flipp

WORKDIR /app

# Install dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Ensure the output and data directories exist and are writable
RUN mkdir -p /data /output && chown -R flipp:flipp /app /data /output

USER flipp

# /data  – SQLite database
# /output – downloaded PDFs
VOLUME ["/data", "/output"]

ENV FLIPP_DB=/data/flipp.db \
    FLIPP_OUTPUT=/output \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# Default: run the web server + scheduler in one process via the
# combined entrypoint. Override CMD to run just the CLI instead.
CMD ["python", "-m", "flipp_dl.web.main"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')"
