FROM python:3.11-slim
WORKDIR /app
COPY requirements-node.txt .
RUN pip install --disable-pip-version-check --timeout 120 --no-cache-dir -r requirements-node.txt && useradd --uid 10001 --create-home storage && mkdir /data && chown storage:storage /data
COPY storage_node ./storage_node
COPY metadata_service ./metadata_service
COPY gateway_service ./gateway_service
COPY scripts ./scripts
COPY metadata_stub.py .
USER storage
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "storage_node.app:app", "--host", "0.0.0.0", "--port", "8000"]
