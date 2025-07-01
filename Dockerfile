FROM python:slim

WORKDIR /app
COPY requirements.lock ./
COPY pyproject.toml ./
COPY README.md ./
RUN PYTHONDONTWRITEBYTECODE=1 pip install --no-cache-dir -r requirements.lock

COPY src .
CMD airport_test_server