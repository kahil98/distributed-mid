from concurrent.futures import ThreadPoolExecutor
import errno
import hashlib
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from storage_node.app import Settings, chunk_path, create_app


def digest(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def node(tmp_path):
    settings = Settings(data_dir=tmp_path, heartbeat_interval=0.03, max_chunk_bytes=1024)
    app = create_app(settings, httpx.MockTransport(lambda r: httpx.Response(200)))
    with TestClient(app) as client:
        yield client, tmp_path


@pytest.mark.parametrize("data", [b"", b"hello", bytes(range(256)), b"x" * 1024])
def test_round_trip_and_idempotence(node, data):
    client, root = node
    key = digest(data)
    url = f"/chunks/{key}"
    assert client.put(url, content=data).status_code == 201
    assert chunk_path(root / "chunks", key).read_bytes() == data
    assert client.put(url, content=data).status_code == 200
    response = client.get(url)
    assert response.status_code == 200
    assert response.content == data
    assert response.headers["etag"] == f'"{key}"'
    response = client.head(url)
    assert response.status_code == 200 and response.content == b""
    assert int(response.headers["content-length"]) == len(data)
    assert client.get("/health").json()["chunk_count"] == 1
    assert client.delete(url).status_code == 204
    assert client.delete(url).status_code == 204
    assert client.get(url).status_code == 404
    assert client.head(url).status_code == 404
    assert client.get("/health").json()["chunk_count"] == 0


@pytest.mark.parametrize("method", ["put", "get", "head", "delete"])
@pytest.mark.parametrize("key", ["invalid", "a" * 63, "A" * 64, "g" * 64])
def test_reject_invalid_identifiers(node, method, key):
    assert getattr(node[0], method)(f"/chunks/{key}").status_code == 400


def test_mismatch_does_not_overwrite_existing_chunk(node):
    client, root = node
    key = digest(b"good")
    url = f"/chunks/{key}"
    assert client.put(url, content=b"good").status_code == 201
    assert client.put(url, content=b"bad").status_code == 422
    assert client.get(url).content == b"good"
    assert list((root / "staging").iterdir()) == []


@pytest.mark.parametrize("method", ["get", "head"])
def test_corruption_rejected_and_repairable(node, method):
    client, root = node
    key = digest(b"good")
    url = f"/chunks/{key}"
    client.put(url, content=b"good")
    chunk_path(root / "chunks", key).write_bytes(b"evil")
    response = getattr(client, method)(url)
    assert response.status_code == 409
    assert b"evil" not in response.content
    assert client.put(url, content=b"good").status_code == 200
    assert client.get(url).content == b"good"


def test_oversize_and_streamed_oversize_leave_no_files(node):
    client, root = node
    data = b"x" * 1025
    url = f"/chunks/{digest(data)}"
    assert client.put(url, content=data).status_code == 413
    assert client.put(url, content=iter([data[:500], data[500:]])).status_code == 413
    assert not list((root / "staging").iterdir())
    assert client.get("/health").json()["chunk_count"] == 0


def test_concurrent_puts_are_atomic_and_deduplicated(node):
    client, _ = node
    data = b"parallel"
    url = f"/chunks/{digest(data)}"
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda _: client.put(url, content=data).status_code, range(24)))
    assert statuses.count(201) == 1
    assert statuses.count(200) == 23
    assert client.get(url).content == data
    assert client.get("/health").json()["chunk_count"] == 1


def test_persists_across_app_restart(tmp_path):
    settings = Settings(data_dir=tmp_path)
    transport = httpx.MockTransport(lambda r: httpx.Response(200))
    url = f"/chunks/{digest(b'persistent')}"
    with TestClient(create_app(settings, transport)) as client:
        assert client.put(url, content=b"persistent").status_code == 201
    with TestClient(create_app(settings, transport)) as client:
        assert client.get(url).content == b"persistent"
        assert client.get("/health").json()["chunk_count"] == 1


def test_disk_full_is_reported_and_staging_is_cleaned(node, monkeypatch):
    client, root = node

    def fail(*args):
        raise OSError(errno.ENOSPC, "full")

    monkeypatch.setattr("storage_node.app.os.replace", fail)
    assert client.put(f"/chunks/{digest(b'x')}", content=b"x").status_code == 507
    assert not list((root / "staging").iterdir())


def test_heartbeat_retries_and_reports_counts(tmp_path):
    messages = []

    def receive(request):
        messages.append(json.loads(request.content))
        return httpx.Response(503 if len(messages) == 1 else 200)

    settings = Settings(node_id="test-node", data_dir=tmp_path, heartbeat_interval=0.03)
    with TestClient(create_app(settings, httpx.MockTransport(receive))) as client:
        assert client.put(f"/chunks/{digest(b'x')}", content=b"x").status_code == 201
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if len(messages) >= 3 and messages[-1]["chunk_count"] == 1:
                break
            time.sleep(0.02)
        assert len(messages) >= 3
        assert messages[-1]["node_id"] == "test-node"
        assert messages[-1]["free_space"] > 0
        assert messages[-1]["chunk_count"] == 1


def test_snapshot_cannot_leak_modified_content(node):
    from storage_node.app import snapshot

    client, root = node
    data = b"verified bytes"
    key = digest(data)
    client.put(f"/chunks/{key}", content=data)
    path = chunk_path(root / "chunks", key)
    copied, size = snapshot(path, key)
    try:
        path.write_bytes(b"tampered later")
        assert copied.read() == data and size == len(data)
    finally:
        copied.close()
