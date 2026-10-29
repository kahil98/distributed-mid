"""Bounded live API check for the user gateway; writes a compact exam report."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid

import requests

BASE = os.getenv("GATEWAY_URL", "http://127.0.0.1:8080")
results = []


def check(condition, name):
    if not condition:
        raise AssertionError(name)
    results.append(name)
    print("PASS", name, flush=True)


def main():
    session = requests.Session()
    session.trust_env = False
    unauth = session.get(f"{BASE}/v1")
    check(unauth.status_code == 401, "JWT is required on user API routes")
    login = session.post(f"{BASE}/auth/token", json={
        "username": os.getenv("GATEWAY_USERNAME", "exam-user"),
        "password": os.getenv("GATEWAY_PASSWORD", "change-me")})
    login.raise_for_status()
    session.headers["Authorization"] = "Bearer " + login.json()["access_token"]
    check(session.get(f"{BASE}/v1").status_code == 200, "JWT login and API interface")
    bucket = "exam-gw-" + uuid.uuid4().hex[:12]
    key = "evidence/hello.txt"
    created = session.post(f"{BASE}/v1/buckets", json={"bucket": bucket})
    created.raise_for_status()
    check(any(row["bucket"] == bucket for row in session.get(f"{BASE}/v1/buckets").json()), "Create and list bucket")
    payload = b"Gateway task 4 range and metadata test."
    upload = session.put(f"{BASE}/v1/buckets/{bucket}/objects/{key}",
                         data=iter([payload[:11], payload[11:]]),
                         headers={"Content-Type": "text/plain", "X-Object-Tags": '{"class":"exam","type":"text"}'})
    upload.raise_for_status()
    check(upload.json()["size"] == len(payload), "Streaming upload records correct object size")
    metadata = session.get(f"{BASE}/v1/buckets/{bucket}/objects/{key}")
    metadata.raise_for_status()
    check(metadata.json()["tags"] == {"class": "exam", "type": "text"}, "Metadata returns user tags")
    search = session.get(f"{BASE}/v1/buckets/{bucket}/objects", params={"prefix": "evidence/", "min_size": 1,
                          "max_size": 1000, "tags": json.dumps({"class": "exam"})})
    search.raise_for_status()
    check(search.json()["count"] == 1 and search.json()["objects"][0]["key"] == key,
          "Elasticsearch prefix, size, and tag metadata search")
    partial = session.get(f"{BASE}/v1/download/{bucket}/{key}", headers={"Range": "bytes=0-6"})
    check(partial.status_code == 206 and partial.content == payload[:7] and partial.headers.get("Content-Range") == f"bytes 0-6/{len(payload)}",
          "Byte-range download returns correct 206 slice")
    full = session.get(f"{BASE}/v1/download/{bucket}/{key}")
    check(full.status_code == 200 and full.content == payload, "Full download returns original payload")
    cluster = session.get(f"{BASE}/v1/cluster/status")
    cluster.raise_for_status()
    cluster_body = cluster.json()
    check(cluster_body["node_count"] >= 6 and "degraded_objects" in cluster_body
          and "repair_progress" in cluster_body
          and all("managed_fragment_count" in node and "health" in node for node in cluster_body["nodes"]),
          "Cluster health reports node state, chunk distribution, and repair progress")
    deleted = session.delete(f"{BASE}/v1/buckets/{bucket}/objects/{key}")
    check(deleted.status_code == 204, "Object deletion returns 204")
    absent = session.get(f"{BASE}/v1/buckets/{bucket}/objects", params={"prefix": "evidence/"})
    absent.raise_for_status()
    check(absent.json()["total"] == 0, "Delete removes the metadata document from Elasticsearch search")
    report = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "checks": results,
              "bucket": bucket, "object_key": key}
    root = Path(__file__).resolve().parents[1]
    for name in ("task-4-verification.json", "task-5-verification.json"):
        output = root / "reports" / name
        output.write_text(json.dumps(report, indent=2) + "\n")
        print(f"Saved report: {output}")


if __name__ == "__main__":
    main()
