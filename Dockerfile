FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY frontend frontend
EXPOSE 8000
# Single worker on purpose: the waiting-room admitter, sweeper and outbox relay run
# in-process and the rate limiter is in-memory. See "Scaling out" in the README.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "30"]
