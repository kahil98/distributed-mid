"""Synthetic file workloads against chunk endpoints, without an S3 coordinator."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
import statistics
import threading
import time

import requests

LOCAL = threading.local()
CHUNK_SIZE = 4 * 1024 * 1024
ROOT = Path(__file__).resolve().parents[1]


def session():
    if not hasattr(LOCAL, "session"):
        LOCAL.session = requests.Session()
        LOCAL.session.trust_env = False
    return LOCAL.session


def exercise_file(spec, cleanup):
    index, size = spec
    rng = random.Random(20261028 + index)
    original = hashlib.sha256()
    restored = hashlib.sha256()
    manifest = []
    put_times, get_times = [], []
    remaining = size
    while remaining:
        data = rng.randbytes(min(remaining, CHUNK_SIZE))
        original.update(data)
        key = hashlib.sha256(data).hexdigest()
        node = (index + len(manifest)) % 4
        url = f"http://127.0.0.1:{8201+node}/chunks/{key}"
        # Track before PUT so interrupted/failed requests also get cleaned up.
        manifest.append((url, key))
        cleanup.append(url)
        started = time.perf_counter()
        response = session().put(url, data=data, timeout=60)
        response.raise_for_status()
        put_times.append(time.perf_counter() - started)
        remaining -= len(data)
    restored_size = 0
    for url, key in manifest:
        started = time.perf_counter()
        response = session().get(url, timeout=60)
        response.raise_for_status()
        data = response.content
        assert hashlib.sha256(data).hexdigest() == key, "Chunk hash mismatch"
        restored.update(data)
        restored_size += len(data)
        get_times.append(time.perf_counter() - started)
    assert restored_size == size, "Reassembled file length mismatch"
    assert original.digest() == restored.digest(), "Reassembled file SHA-256 mismatch"
    return {"bytes": size, "chunks": len(manifest), "put": put_times, "get": get_times}


def workload(name, specs, workers):
    results = []
    cleanup = []
    started = time.perf_counter()
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for result in pool.map(lambda spec: exercise_file(spec, cleanup), specs):
                results.append(result)
                if len(results) % 500 == 0 or len(specs) <= 10:
                    print(f"{name}: {len(results)}/{len(specs)} files verified", flush=True)
    finally:
        def remove(url):
            session().delete(url, timeout=60).raise_for_status()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(remove, cleanup))
    elapsed = time.perf_counter() - started
    total = sum(r["bytes"] for r in results)
    summary = {
        "files": len(results), "bytes": total,
        "chunks": sum(r["chunks"] for r in results),
        "workers": workers, "elapsed_seconds": round(elapsed, 3),
        "roundtrip_mib_per_second_including_generation_and_cleanup": round(2 * total / 1024**2 / elapsed, 3),
        "all_file_and_chunk_hashes_verified": True,
    }
    for method in ("put", "get"):
        latencies = sorted(t * 1000 for r in results for t in r[method])
        summary[method + "_latency_ms"] = {
            "median": round(statistics.median(latencies), 3),
            "p95": round(latencies[min(len(latencies)-1, int(len(latencies) * 0.95))], 3),
        }
    print(json.dumps({name: summary}, indent=2), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--small-files", type=int, default=5000)
    parser.add_argument("--large-files", type=int, default=10)
    parser.add_argument("--large-mib", type=int, default=100)
    args = parser.parse_args()
    if min(args.small_files, args.large_files, args.large_mib) <= 0:
        parser.error("Workload sizes must be positive")
    rng = random.Random(42)
    report = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Storage-node chunk API; client-side fixed-size splitting and reassembly; no replication, erasure coding, or S3 API",
        "chunk_size_bytes": CHUNK_SIZE,
        "small_files": workload("small_files", [(i, rng.randint(1024, 50 * 1024)) for i in range(args.small_files)], 8),
        "large_files": workload("large_files", [(100000+i, args.large_mib * 1024**2) for i in range(args.large_files)], 4),
    }
    target = ROOT / "reports" / "workloads.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Report saved to {target}")


if __name__ == "__main__":
    main()
