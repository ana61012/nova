# --- build the Angular frontend
FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- API + built frontend
FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY --from=web /web/dist/nova/browser frontend/dist/nova/browser
EXPOSE 8000
# Single worker on purpose: the waiting-room admitter, sweeper and outbox relay run
# in-process and the rate limiter is in-memory. See "Scaling out" in the README.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "30"]