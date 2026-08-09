FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app
COPY requirements.txt ./
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && useradd --create-home --uid 10001 vipercapture \
    && mkdir -p /data /app/captures \
    && chown -R vipercapture:vipercapture /data /app/captures /ms-playwright

COPY --chown=vipercapture:vipercapture . .
USER vipercapture
ENV VIPERCAPTURE_DATA_DIR=/data \
    VIPERCAPTURE_CAPTURES_DIR=/app/captures
EXPOSE 8000
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2)" || exit 1
CMD ["uvicorn", "vipercapture.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
