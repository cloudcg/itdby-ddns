FROM python:3.9-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY itdby_ddns.py .

RUN mkdir -p /app/config /app/logs

STOPSIGNAL SIGTERM

CMD ["python", "itdby_ddns.py"]
