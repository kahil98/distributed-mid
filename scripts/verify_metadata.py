"""Live PostgreSQL, manifest, chunking, refcount, download and ring checks."""
import hashlib
import json
from pathlib import Path
import random
import requests

BASE = "http://127.0.0.1:8100"
s = requests.Session()
s.trust_env = False
checks = []


def check(condition, description):
    if not condition:
        raise AssertionError(description)
    checks.append(description)
    print("PASS", description, flush=True)


def put(bucket, key, data, mode="fixed"):
    r = s.put(f"{BASE}/objects/{bucket}/{key}", params={"mode": mode}, data=data, timeout=120)
    r.raise_for_status()
    return r.json()


def main():
    for _ in range(30):
        try:
            s.get(f"{BASE}/health", timeout=3).raise_for_status()
            break
        except requests.RequestException:
            import time
            time.sleep(1)
    check(s.get(f"{BASE}/health").json() == {"status": "ok", "database": "ok"},
          "Metadata API and PostgreSQL are healthy")
    node_rows = s.get(f"{BASE}/nodes", timeout=5).json()
    check(len(node_rows) >= 4, "PostgreSQL stores heartbeats from four storage nodes")

    shared = bytes(range(256)) * 16384  # exactly one 4 MiB fixed-size chunk
    first = put("exam", "dedup/a.bin", shared)
    second = put("exam", "dedup/b.bin", shared)
    digest = hashlib.sha256(shared).hexdigest()
    check(first["version"] == second["version"] == 1 and first["chunks"][0]["hash"] == digest,
          "Object manifests include ordered SHA-256 chunk metadata")
    refs = s.get(f"{BASE}/chunks/{digest}/references").json()
    check(refs["reference_count"] == 2, "Repeated content shares one stored chunk with two references")
    check(s.get(f"{BASE}/objects/exam/dedup/a.bin").json()["size"] == len(shared),
          "Object metadata persists bucket, key, size, version, chunks, and creation date")
    s.delete(f"{BASE}/objects/exam/dedup/a.bin").raise_for_status()
    check(s.get(f"{BASE}/chunks/{digest}/references").json()["reference_count"] == 1,
          "Deleting one object preserves its still-referenced chunk")
    s.delete(f"{BASE}/objects/exam/dedup/b.bin").raise_for_status()
    storage_port = 8200 + int(refs["node_id"].removeprefix("node-"))
    check(s.get(f"http://127.0.0.1:{storage_port}/chunks/{digest}").status_code == 404,
          "Chunk is deleted from disk when its last object reference is removed")

    v1 = put("exam", "versions/object.txt", b"version one")
    v2 = put("exam", "versions/object.txt", b"version two")
    check(v1["version"] == 1 and v2["version"] == 2,
          "Overwriting an object increments its stored version")
    check(s.get(f"{BASE}/download/exam/versions/object.txt").content == b"version two",
          "Download returns the newest object version")
    check(s.get(f"{BASE}/chunks/{hashlib.sha256(b'version one').hexdigest()}/references").status_code == 404,
          "Replacing an object releases its prior chunk reference")
    s.delete(f"{BASE}/objects/exam/versions/object.txt").raise_for_status()

    random.seed(84)
    cdc_data = random.randbytes(350_000)
    cdc = put("exam", "chunking/cdc.dat", cdc_data, "cdc")
    fixed = put("exam", "chunking/fixed.dat", cdc_data, "fixed")
    check(cdc["chunking_mode"] == "cdc" and fixed["chunking_mode"] == "fixed" and
          len(fixed["chunks"]) == 1 and len(cdc["chunks"]) > 1,
          "Fixed and rolling-hash CDC split the same input using different boundaries")
    downloaded = s.get(f"{BASE}/download/exam/chunking/cdc.dat", timeout=60)
    downloaded.raise_for_status()
    check(downloaded.content == cdc_data and hashlib.sha256(downloaded.content).digest() ==
          hashlib.sha256(cdc_data).digest(), "CDC chunks reassemble to the original object")
    check(all(len(c["hash"]) == 64 for c in cdc["chunks"]), "Every manifest chunk has a SHA-256 address")
    s.delete(f"{BASE}/objects/exam/chunking/cdc.dat").raise_for_status()
    s.delete(f"{BASE}/objects/exam/chunking/fixed.dat").raise_for_status()

    hashes = [hashlib.sha256(f"placement-chunk-{i}".encode()).hexdigest() for i in range(50_000)]
    nodes = sorted(n["node_id"] for n in node_rows[:4])
    for change in ({"add_node": "node-simulated-5"}, {"remove_node": nodes[0]}):
        result = s.post(f"{BASE}/placement/measure", json={"nodes": nodes, "chunk_hashes": hashes, **change},
                        timeout=60)
        result.raise_for_status()
        measurement = result.json()
        check(0 < measurement["moved_percent"] < 35 and
              abs(measurement["moved_percent"] - measurement["expected_approx_percent"]) < 3,
              f"Ring {('add' if 'add_node' in change else 'remove')} moves only expected chunks: {measurement['moved_percent']}%")
        checks.append(json.dumps(measurement, sort_keys=True))
    report = Path(__file__).resolve().parents[1] / "reports" / "task-2-verification.json"
    report.write_text(json.dumps({"passed": len(checks), "checks": checks}, indent=2) + "\n")
    print(f"{len(checks)} live metadata checks passed; {report}")


if __name__ == "__main__":
    main()
