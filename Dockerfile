FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/data/chat.db \
    PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py manage.py ./
COPY templates ./templates
COPY static ./static

# Run as a non-root user; /data holds the SQLite database (mount a volume here).
RUN useradd --create-home --uid 1000 appuser \
    && mkdir /data && chown appuser:appuser /data
USER appuser
VOLUME /data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ['PORT'], timeout=3)"

# One worker + threads: SQLite-friendly, and threads handle concurrent streaming replies.
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT} --workers 1 --threads 8 --timeout 120 --access-logfile - app:app"]
