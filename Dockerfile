FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
# rawpy (DNG support) has no wheel for some NAS CPUs; the app still runs without it.
RUN grep -v rawpy requirements.txt > /tmp/req.txt \
    && pip install --no-cache-dir -r /tmp/req.txt \
    && (pip install --no-cache-dir rawpy || echo "rawpy unavailable: DNG import disabled")
COPY app ./app

ENV SIRIS_DATA_DIR=/data \
    SIRIS_OUTPUT_ROOTS="NAS=/nas/exports" \
    PYTHONUNBUFFERED=1
RUN mkdir -p /data /nas/exports
VOLUME ["/data"]
EXPOSE 8080
CMD ["python", "-m", "app", "--host", "0.0.0.0", "--port", "8080"]
