"""Short live check for replication, quorum, EC recovery, scrub and workload overhead."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
import subprocess
import time
import requests

BASE = "http://127.0.0.1:8100"
s = requests.Session()
s.trust_env = False
results = []


def check(ok, message):
    if not ok:
        raise AssertionError(message)
    results.append(message)
    print("PASS", message, flush=True)


def set_policy(bucket, policy):
    r = s.put(f"{BASE}/buckets/{bucket}/policy", json=policy, timeout=10)
    r.raise_for_status()
    return r.json()


def upload(bucket, key, data):
    r = s.put(f"{BASE}/objects/{bucket}/{key}", data=data, timeout=60)
    r.raise_for_status()
    return r.json()


def download(bucket, key, data):
    r = s.get(f"{BASE}/download/{bucket}/{key}", timeout=60)
    r.raise_for_status()
    assert r.content == data and hashlib.sha256(r.content).digest() == hashlib.sha256(data).digest()


def port(node_id):
    return 8200 + int(node_id.rsplit("-", 1)[1])


def benchmark(mode, workload, sizes, seed):
    bucket = f"task3-{mode}-{workload}"
    if mode == "replication":
        policy = {"mode": mode, "n": 3, "w": 2, "r": 2}
    else:
        policy = {"mode": mode, "k": 4, "m": 2, "w": 4, "r": 4}
    set_policy(bucket, policy)
    rng = random.Random(seed)
    logical = physical = 0
    started = time.perf_counter()
    for i, size in enumerate(sizes):
        data = rng.randbytes(size)
        obj = upload(bucket, f"file-{i}.bin", data)
        logical += size
        physical += sum(f["size"] for chunk in obj["chunks"] for f in chunk["fragments"])
    elapsed = time.perf_counter() - started
    for i, size in enumerate(sizes):
        # Independent seed stream reconstructs each deterministic synthetic file.
        data = random.Random(seed + i * 0 + 0).randbytes(size) if len(sizes) == 1 else None
        if data is None:
            data_rng = random.Random(seed)
            for _ in range(i + 1):
                data = data_rng.randbytes(sizes[_])
        download(bucket, f"file-{i}.bin", data)
    for i in range(len(sizes)):
        s.delete(f"{BASE}/objects/{bucket}/file-{i}.bin", timeout=20).raise_for_status()
    parity = 3 if mode == "replication" else 4
    n = 3 if mode == "replication" else 6
    repair_estimate = (2 * logical / n) if mode == "replication" else logical * (1 + 1 / parity)
    return {"mode": mode, "workload": workload, "files": len(sizes), "logical_bytes": logical,
            "stored_fragment_bytes": physical, "storage_overhead_ratio": round(physical / max(1, logical), 4),
            "upload_seconds": round(elapsed, 3),
            "estimated_one_node_repair_traffic_bytes": int(repair_estimate)}


def main():
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            s.get(f"{BASE}/health", timeout=2).raise_for_status()
            break
        except requests.RequestException:
            time.sleep(1)
    nodes = s.get(f"{BASE}/nodes", timeout=5).json()
    check(len(nodes) >= 6, "Six live storage nodes are available for 4+2")
    invalid = s.put(f"{BASE}/buckets/bad-quorum/policy",
                    json={"mode":"replication","n":3,"w":1,"r":2}, timeout=5)
    check(invalid.status_code == 422, "W + R <= N is rejected")

    rep_data = random.Random(101).randbytes(300_000)
    set_policy("task3-repair-rep", {"mode":"replication","n":3,"w":2,"r":2})
    rep_obj = upload("task3-repair-rep", "payload.bin", rep_data)
    rep_chunk = rep_obj["chunks"][0]
    check(len(rep_chunk["fragments"]) == 3, "Replication stores three full chunk copies")
    failed_node = rep_chunk["fragments"][0]["node_id"]
    fail = s.post(f"{BASE}/admin/nodes/{failed_node}/fail", timeout=5)
    fail.raise_for_status()
    repair = s.post(f"{BASE}/admin/repair/run", timeout=30)
    repair.raise_for_status()
    check(repair.json()["repaired"] >= 1, "Failed replica is rebuilt on a surviving node")
    s.post(f"{BASE}/admin/nodes/{failed_node}/recover", timeout=5).raise_for_status()
    download("task3-repair-rep", "payload.bin", rep_data)
    check(True, "Replicated object remains readable after one node failure and repair")

    ec_data = random.Random(202).randbytes(300_000)
    set_policy("task3-scrub-ec", {"mode":"erasure","k":4,"m":2,"w":4,"r":4})
    ec_obj = upload("task3-scrub-ec", "payload.bin", ec_data)
    ec_chunk = ec_obj["chunks"][0]
    check(len(ec_chunk["fragments"]) == 6, "4+2 Reed-Solomon stores six fragments")
    lost = ec_chunk["fragments"][0]
    service = lost["node_id"]
    shard_path = f"/data/chunks/{lost['fragment_hash'][:2]}/{lost['fragment_hash']}"
    corrupt_script = (
        "from pathlib import Path; p=Path(" + repr(shard_path) + "); "
        "f=p.open('r+b'); b=f.read(1); f.seek(0); f.write(bytes([b[0]^1])); f.close()"
    )
    subprocess.run(["docker", "compose", "-f", "compose.storage.yaml", "exec", "-T",
                    service, "python", "-c", corrupt_script], check=True, timeout=10)
    corrupt_response = s.get(
        f"http://127.0.0.1:{port(lost['node_id'])}/chunks/{lost['fragment_hash']}", timeout=5
    )
    check(corrupt_response.status_code == 409, "Storage node rejects a bit-rotted fragment by SHA-256")
    scrub = s.post(f"{BASE}/admin/scrub/run", timeout=30)
    scrub.raise_for_status()
    check(scrub.json()["corrupt"] >= 1, "Background scrub detects a bit-rotted shard")
    repair = s.post(f"{BASE}/admin/repair/run", timeout=30)
    repair.raise_for_status()
    check(repair.json()["repaired"] >= 1, "Scrubbed erasure shard is reconstructed from k healthy shares")
    download("task3-scrub-ec", "payload.bin", ec_data)
    check(True, "4+2 object reconstructs byte-for-byte after scrub repair")
    s.delete(f"{BASE}/objects/task3-repair-rep/payload.bin", timeout=20).raise_for_status()
    s.delete(f"{BASE}/objects/task3-scrub-ec/payload.bin", timeout=20).raise_for_status()

    report = {"timestamp_utc": datetime.now(timezone.utc).isoformat(),
              "checks": results,
              "recovery_traffic_bytes": s.get(f"{BASE}/reliability/stats", timeout=5).json(),
              "workloads": []}
    report["workloads"] += [
        benchmark(mode, "many_small_sample", [random.Random(300+i).randint(1024,8*1024) for i in range(12)], 333)
        for mode in ("replication", "erasure")
    ]
    report["workloads"] += [
        benchmark(mode, "few_large_sample", [64*1024, 96*1024], 444)
        for mode in ("replication", "erasure")
    ]
    output = Path(__file__).resolve().parents[1] / "reports" / "task-3-verification.json"
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Saved verification and comparison to {output}")
    print(json.dumps(report["workloads"], indent=2))


if __name__ == "__main__":
    main()
