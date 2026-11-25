# ---------- build stage: wheels کامپایل می‌شن و فقط venv نهایی منتقل می‌شه ----------
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install -r requirements.txt

# ---------- runtime stage ----------
FROM python:3.12-slim
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC
RUN useradd -m -u 1000 app && mkdir -p /app/data && chown -R app:app /app
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=app:app app.py panel_ui.html ./
USER app

# داده‌ها (دیتابیس، نشست‌ها، لاگ) باید روی volume باشن
VOLUME ["/app/data"]
EXPOSE 8787

# پورت از WEB_PORT / PORT / SERVER_PORT خونده می‌شه (مثل خود برنامه)، پیش‌فرض 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request as u;p=next((os.environ[k] for k in ('WEB_PORT','SERVER_PORT','PORT') if os.environ.get(k,'').isdigit()),'8787');u.urlopen('http://127.0.0.1:%s/healthz'%p,timeout=4)" || exit 1

CMD ["python", "app.py"]
