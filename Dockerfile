# Serves two purposes from one image:
#   default command → adapter.py (long-running HTTP query API)
#   command override → refresh.py (weekly login/scrape/download/import;
#   see docker-compose.yml's `refresh` service or run it via cron/CronJob)
FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends mariadb-client && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY adapter.py refresh.py ./

EXPOSE 8080
CMD ["uvicorn", "adapter:app", "--host", "0.0.0.0", "--port", "8080"]
