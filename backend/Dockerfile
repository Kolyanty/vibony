# DEV image only — do NOT deploy. Installs dev deps, runs uvicorn
# --reload + `alembic upgrade head` in CMD, and runs as root. Production: Dockerfile.prod.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/repo/backend

RUN pip install --no-cache-dir poetry==1.6.1 && \
    poetry config virtualenvs.create false

WORKDIR /repo/backend

# Install deps in a separate layer so source changes don't bust the cache
COPY backend/pyproject.toml backend/poetry.lock ./
RUN poetry install --no-interaction --no-root

COPY backend/ .

CMD ["sh", "-c", "alembic upgrade head && exec uvicorn vibony_backend.main:app --host 0.0.0.0 --port 8000 --reload"]
