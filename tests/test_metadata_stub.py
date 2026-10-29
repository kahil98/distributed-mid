from fastapi.testclient import TestClient

import metadata_stub


def test_registry_updates_and_expiry(monkeypatch):
    metadata_stub.nodes.clear()
    clock = [100.0]
    monkeypatch.setattr(metadata_stub.time, "monotonic", lambda: clock[0])
    with TestClient(metadata_stub.app) as client:
        data = {"node_id": "node-1", "address": "http://node-1:8000", "free_space": 500, "chunk_count": 2}
        assert client.post("/heartbeats", json=data).status_code == 200
        entry = client.get("/nodes").json()[0]
        assert entry["alive"] and entry["chunk_count"] == 2
        clock[0] += 13
        assert not client.get("/nodes").json()[0]["alive"]
        data["chunk_count"] = 3
        client.post("/heartbeats", json=data)
        entries = client.get("/nodes").json()
        assert len(entries) == 1 and entries[0]["alive"] and entries[0]["chunk_count"] == 3
        assert client.post("/heartbeats", json={**data, "free_space": -1}).status_code == 422
    metadata_stub.nodes.clear()
