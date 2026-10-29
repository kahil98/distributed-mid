FROM python:3.11-slim
WORKDIR /app
COPY requirements-node.txt .
RUN pip install --no-cache-dir -r requirements-node.txt && useradd --uid 10001 app
COPY storage_node ./storage_node
USER app
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8000
CMD ["uvicorn","storage_node.app:app","--host","0.0.0.0","--port","8000"]
