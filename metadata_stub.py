"""Heartbeat registry for the storage-node task; not the full metadata module."""
from datetime import datetime, timezone
import time

from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI(title="Development heartbeat registry")
nodes = {}


class Heartbeat(BaseModel):
    node_id: str = Field(min_length=1, max_length=128)
    address: str = Field(min_length=1)
    free_space: int = Field(ge=0)
    chunk_count: int = Field(ge=0)


@app.post("/heartbeats")
def heartbeat(payload: Heartbeat):
    nodes[payload.node_id] = {
        **payload.model_dump(),
        "last_seen": datetime.now(timezone.utc).isoformat(),
        "received_at": time.monotonic(),
    }
    return {"accepted": True}


@app.get("/nodes")
def list_nodes():
    return [
        {k: v for k, v in entry.items() if k != "received_at"}
        | {"alive": time.monotonic() - entry["received_at"] < 12}
        for entry in list(nodes.values())
    ]


@app.get("/health")
def health():
    return {"status": "ok"}
