FROM python:slim

WORKDIR /app
RUN ls -l
COPY requirements.lock ./
RUN PYTHONDONTWRITEBYTECODE=1 pip install --no-cache-dir -r requirements.lock

COPY src .
CMD airport_test_server