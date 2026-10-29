"""RabbitMQ management API worker; safe to scale to multiple consumers."""
import base64
import json
import os
import time
import urllib.error
import urllib.request

RABBIT = os.getenv("RABBITMQ_MANAGEMENT_URL", "http://rabbitmq:15672").rstrip("/")
USER = os.getenv("RABBITMQ_USER", "dev")
PASSWORD = os.getenv("RABBITMQ_PASSWORD", "dev")
METADATA = os.getenv("METADATA_URL", "http://metadata:8000").rstrip("/")
QUEUES = {"repair_jobs": "repair", "rebalancing_jobs": "rebalance", "garbage_collection_jobs": "gc"}
AUTH = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()


def call(url, method="GET", body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"Authorization": AUTH, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def run_job(kind):
    paths = {"repair": "/admin/repair/run", "rebalance": "/admin/rebalance/run", "gc": "/admin/gc/run"}
    request = urllib.request.Request(METADATA + paths[kind], data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=180) as response:
        return response.read().decode()


def main():
    while True:
        worked = False
        for queue, kind in QUEUES.items():
            queue_url = f"{RABBIT}/api/queues/%2F/{queue}"
            try:
                call(queue_url, "PUT", {"durable": True, "auto_delete": False, "arguments": {}})
                messages = call(queue_url + "/get", "POST", {
                    "count": 1, "ackmode": "ack_requeue_false", "encoding": "auto", "truncate": 50000})
                if messages:
                    worked = True
                    print(f"{kind} job result:", run_job(kind), flush=True)
            except (OSError, ValueError, urllib.error.URLError) as exc:
                print(f"{kind} worker retry:", exc, flush=True)
                time.sleep(2)
        if not worked:
            time.sleep(1)


if __name__ == "__main__":
    main()
