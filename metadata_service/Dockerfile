FROM python:3.11-slim
WORKDIR /app
COPY requirements-node.txt .
RUN pip install --no-cache-dir -r requirements-node.txt && useradd --uid 10001 app
COPY metadata_service ./metadata_service
USER app
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn","metadata_service.app:app","--host","0.0.0.0","--port","8000"]
