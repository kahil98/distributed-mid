FROM python:3.11-slim
WORKDIR /app
COPY requirements-node.txt .
RUN pip install --no-cache-dir -r requirements-node.txt && useradd --uid 10001 app
COPY gateway_service ./gateway_service
USER app
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn","gateway_service.app:app","--host","0.0.0.0","--port","8000"]
