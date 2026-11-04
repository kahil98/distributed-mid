import asyncio

from metadata_service import app as metadata_app


class FakeResult:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, nodes):
        self.nodes = nodes

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params):
        if query.lstrip().startswith("SELECT state FROM storage_nodes"):
            state = self.nodes.get(params[0])
            return FakeResult({"state": state} if state else None)
        if query.lstrip().startswith("INSERT INTO storage_nodes"):
            self.nodes[params[0]] = "healthy"
        return FakeResult()


def test_first_storage_node_heartbeat_queues_rebalance_only_once(monkeypatch):
    nodes = {}
    queued = []

    async def enqueue(kind):
        queued.append(kind)

    monkeypatch.setattr(metadata_app, "connect", lambda: FakeConnection(nodes))
    monkeypatch.setattr(metadata_app, "enqueue_job", enqueue)
    heartbeat = {
        "node_id": "node-8",
        "address": "http://node-8:8000",
        "free_space": 10_000,
        "chunk_count": 0,
    }

    first = asyncio.run(metadata_app.heartbeat(heartbeat))
    second = asyncio.run(metadata_app.heartbeat(heartbeat))

    assert first == {"accepted": True, "rebalance_queued": True}
    assert second == {"accepted": True, "rebalance_queued": False}
    assert queued == ["rebalance"]
