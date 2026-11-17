FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/identity.db \
    PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py db.py sample_records.csv ./
COPY templates ./templates
COPY static ./static

RUN mkdir -p /data && chown -R 10001:10001 /data /app
USER 10001

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"

# One worker + threads: the in-process write lock serialises decisions and SQLite
# is the durable store.  Scale out with several workers only if you also accept
# that the DB-level BEGIN IMMEDIATE transaction (not the process lock) carries
# the invariant — which it does, so multiple workers are safe too.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--timeout", "60", "app:app"]
