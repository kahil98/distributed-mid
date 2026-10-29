"""Exercise the actual four-node Compose deployment, including disk tampering."""
import hashlib
import json
from pathlib import Path
import subprocess
import time

import requests

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose", "-f", str(ROOT / "compose.storage.yaml")]
session = requests.Session()
session.trust_env = False
checks = []


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    checks.append(message)
    print("PASS", message, flush=True)


def compose(*args):
    return subprocess.check_output(COMPOSE + list(args), cwd=ROOT, text=True).strip()


def wait_for(predicate, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except requests.RequestException:
            pass
        time.sleep(1)
    raise AssertionError("Timed out waiting for service or heartbeat")


def registry():
    r = session.get("http://127.0.0.1:8100/nodes", timeout=5)
    r.raise_for_status()
    return r.json()


def main():
    ids = [compose("ps", "-q", f"node-{i}") for i in range(1, 5)]
    check(len(set(ids)) == 4 and all(ids), "Four distinct storage containers")
    wait_for(lambda: len(registry()) == 4)
    check(True, "All four nodes send heartbeats to metadata registry")
    data = b"storage-task-container-verification-v1"
    key = hashlib.sha256(data).hexdigest()
    urls = [f"http://127.0.0.1:{8200+i}/chunks/{key}" for i in range(1, 5)]
    for i, url in enumerate(urls, 1):
        session.delete(url, timeout=5).raise_for_status()
        check(session.put(url, data=data, timeout=5).status_code == 201, f"node-{i} PUT")
        r = session.get(url, timeout=5)
        check(r.status_code == 200 and r.content == data, f"node-{i} verified GET")
        r = session.head(url, timeout=5)
        check(r.status_code == 200 and int(r.headers["Content-Length"]) == len(data), f"node-{i} HEAD")
        check(session.put(url, data=b"wrong", timeout=5).status_code == 422, f"node-{i} rejects hash mismatch")
    wait_for(lambda: all(n["chunk_count"] >= 1 and n["free_space"] > 0 for n in registry()))
    check(True, "Heartbeat includes free space and updated chunk count")
    compose("restart", "node-1")
    wait_for(lambda: session.get(urls[0], timeout=5).content == data)
    check(True, "Chunk survives container restart")
    tamper = f"from pathlib import Path; Path('/data/chunks/{key[:2]}/{key}').write_bytes(b'corrupted')"
    compose("exec", "-T", "node-1", "python", "-c", tamper)
    for method in (session.get, session.head):
        check(method(urls[0], timeout=5).status_code == 409, f"Corruption rejected by {method.__name__.upper()}")
    check(session.get(urls[1], timeout=5).content == data, "Corruption on node-1 does not affect node-2 disk")
    check(session.put(urls[0], data=data, timeout=5).status_code == 200, "Valid re-upload repairs corrupt chunk")
    compose("stop", "metadata")
    try:
        time.sleep(4)
        check(session.get(urls[0], timeout=5).content == data, "Node serves chunks during metadata outage")
    finally:
        compose("start", "metadata")
    wait_for(lambda: len(registry()) == 4)
    check(True, "Heartbeats recover after metadata restart")
    for i, url in enumerate(urls, 1):
        check(session.delete(url, timeout=5).status_code == 204, f"node-{i} DELETE")
        check(session.get(url, timeout=5).status_code == 404, f"node-{i} deleted chunk is absent")
    wait_for(lambda: all(n["chunk_count"] == 0 for n in registry()))
    check(True, "Heartbeat chunk counts reflect deletion")
    report = ROOT / "reports" / "container-verification.json"
    report.parent.mkdir(exist_ok=True)
    report.write_text(json.dumps({"passed": len(checks), "checks": checks}, indent=2) + "\n")
    print(f"{len(checks)} checks passed; {report}")


if __name__ == "__main__":
    main()
