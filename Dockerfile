# Sync service image. Runs once and exits; schedule it from outside
# (cron, launchd, or a scheduled job), do not loop inside the container.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ ./src/
ENV PYTHONPATH=/app/src
ENTRYPOINT ["python", "-m", "garmin_sync"]
CMD ["--days", "7"]
