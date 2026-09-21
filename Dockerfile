# Build React first, then run FastAPI and serve the compiled frontend from the same origin.
FROM node:22-alpine AS frontend-build
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FRONTEND_DIST=/app/frontend_dist
WORKDIR /app/backend
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./
COPY --from=frontend-build /frontend/dist /app/frontend_dist
EXPOSE 10000
# init_db is idempotent: it only creates the first admin if the database has no users.
CMD ["/bin/sh", "-c", "python -m app.init_db && exec python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000}"]
