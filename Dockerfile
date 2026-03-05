FROM mcr.microsoft.com/playwright/python:latest
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt && python -m playwright install
ENV PYTHONUNBUFFERED=1
CMD ["python", "task_worker/worker.py", "--batch-size", "5", "--lease-minutes", "15", "--poll-seconds", "10", "--headless"]
