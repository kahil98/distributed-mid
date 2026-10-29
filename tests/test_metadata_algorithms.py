import hashlib

import pytest

from metadata_service.chunking import split_chunks
from metadata_service.ring import ConsistentHashRing
from metadata_service.app import measure_placement


def test_fixed_chunks_have_requested_size_and_reassemble():
    data = bytes(range(251)) * 100
    chunks = list(split_chunks([data[:137], data[137:5000], data[5000:]], "fixed", fixed_size=4096))
    assert b"".join(chunks) == data
    assert [len(c) for c in chunks] == [4096] * 6 + [len(data) - 4096 * 6]


def test_cdc_is_stream_boundary_independent_and_bounded():
    data = (b"the quick brown fox jumps over the lazy dog\n" * 12000)
    one = list(split_chunks([data], "cdc", minimum=1024, average=4096, maximum=8192))
    split = list(split_chunks([data[i:i+333] for i in range(0, len(data), 333)],
                              "cdc", minimum=1024, average=4096, maximum=8192))
    assert one == split
    assert b"".join(one) == data
    assert all(len(c) <= 8192 for c in one)
    assert all(len(c) >= 1024 for c in one[:-1])
    assert len(one) > 20


def test_cdc_boundaries_survive_local_insertions():
    original = __import__("random").Random(10).randbytes(500_000)
    changed = original[:200_000] + b"hello" * 100 + original[200_000:]
    chunks_a = list(split_chunks([original], "cdc", minimum=1024, average=4096, maximum=8192))
    chunks_b = list(split_chunks([changed], "cdc", minimum=1024, average=4096, maximum=8192))
    hashes_a = {hashlib.sha256(c).hexdigest() for c in chunks_a}
    hashes_b = {hashlib.sha256(c).hexdigest() for c in chunks_b}
    assert len(hashes_a & hashes_b) >= len(hashes_a) * 0.75


@pytest.mark.parametrize("mode", ["invalid", ""])
def test_rejects_unknown_chunking_mode(mode):
    with pytest.raises(ValueError):
        list(split_chunks([b"hello"], mode))


def test_ring_is_stable_and_adding_a_node_moves_only_to_the_new_node():
    keys = [hashlib.sha256(f"chunk-{i}".encode()).hexdigest() for i in range(100_000)]
    ring = ConsistentHashRing([f"node-{i}" for i in range(4)], 128)
    placements = {key: ring.get_node(key) for key in keys}
    stable = ConsistentHashRing([f"node-{i}" for i in reversed(range(4))], 128)
    assert placements == {key: stable.get_node(key) for key in keys}
    ring.add_node("node-4")
    moved = [key for key in keys if placements[key] != ring.get_node(key)]
    assert moved and all(ring.get_node(key) == "node-4" for key in moved)
    assert 16 <= len(moved) / len(keys) * 100 <= 24


def test_ring_removal_moves_only_removed_nodes_and_rejects_empty():
    keys = [hashlib.sha256(f"object-chunk-{i}".encode()).hexdigest() for i in range(100_000)]
    ring = ConsistentHashRing([f"node-{i}" for i in range(4)], 128)
    before = {key: ring.get_node(key) for key in keys}
    expected = {key for key in keys if before[key] == "node-2"}
    assert ring.remove_node("node-2")
    moved = {key for key in keys if before[key] != ring.get_node(key)}
    assert moved == expected
    for node in list(ring.nodes):
        ring.remove_node(node)
    with pytest.raises(LookupError):
        ring.get_node("chunk")


def test_ring_rejects_zero_virtual_nodes():
    with pytest.raises(ValueError):
        ConsistentHashRing([], 0)


def test_measurement_reports_correct_expected_share_on_add_and_remove():
    nodes = [f"node-{i}" for i in range(4)]
    hashes = [hashlib.sha256(f"measure-{i}".encode()).hexdigest() for i in range(50_000)]
    add = measure_placement({"nodes": nodes, "chunk_hashes": hashes, "add_node": "node-4"})
    remove = measure_placement({"nodes": nodes, "chunk_hashes": hashes, "remove_node": "node-2"})
    assert add["expected_approx_percent"] == 20
    assert abs(add["moved_percent"] - 20) < 3
    assert remove["expected_approx_percent"] == 25
    assert abs(remove["moved_percent"] - 25) < 3
