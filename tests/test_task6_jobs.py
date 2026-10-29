import asyncio
import json

from metadata_service import app as metadata
from scripts import job_worker


def test_queue_publisher_routes_each_job_type(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"routed": True}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def put(self, url, json):
            calls.append(("put", url, json))
            return Response()

        async def post(self, url, json):
            calls.append(("post", url, json))
            return Response()

    monkeypatch.setattr(metadata.httpx, "AsyncClient", lambda **kwargs: Client())
    for kind, expected_queue in metadata.JOB_QUEUES.items():
        result = asyncio.run(metadata.enqueue_job(kind))
        assert result["queue"] == expected_queue
    assert len(calls) == 6
    assert all(call[2]["durable"] for call in calls[::2])
    assert all(json.loads(call[2]["payload"])["type"] in metadata.JOB_QUEUES for call in calls[1::2])


def test_worker_dispatches_to_matching_metadata_handler(monkeypatch):
    received = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return b"ok"

    def fake_urlopen(request, timeout):
        received.append((request.full_url, request.method, timeout))
        return Response()

    monkeypatch.setattr(job_worker.urllib.request, "urlopen", fake_urlopen)
    for kind, path in (("repair", "/admin/repair/run"), ("rebalance", "/admin/rebalance/run"), ("gc", "/admin/gc/run")):
        assert job_worker.run_job(kind) == "ok"
        assert received[-1][0].endswith(path)
        assert received[-1][1] == "POST"
        assert received[-1][2] == 180
