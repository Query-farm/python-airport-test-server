FROM python:slim

EXPOSE 50003

FROM python:slim
RUN pip install uv
RUN --mount=source=dist,target=/dist uv pip install --system --no-cache /dist/*.whl
CMD airport_test_server --location=grpc://0.0.0.0:50003

