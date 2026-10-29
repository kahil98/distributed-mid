import itertools
import random

import pytest

from metadata_service.reliability import decode, encode, validate_policy


def test_replication_defaults_and_quorum_validation():
    policy = validate_policy({})
    assert (policy["n"], policy["w"], policy["r"]) == (3, 2, 2)
    for invalid in ({"n": 3, "w": 1, "r": 2},
                    {"mode": "erasure", "k": 4, "m": 2, "w": 3, "r": 4}):
        with pytest.raises(ValueError):
            validate_policy(invalid)


@pytest.mark.parametrize("length", [0, 1, 7, 15, 16, 127, 4097, 65535])
def test_reed_solomon_4_plus_2_recovers_every_four_share_subset(length):
    policy = validate_policy({"mode": "erasure", "k": 4, "m": 2, "w": 4, "r": 4})
    data = random.Random(length + 7).randbytes(length)
    shares, padding = encode(data, policy)
    assert sum(map(len, shares)) >= len(data)
    for indices in itertools.combinations(range(6), 4):
        rebuilt = decode([shares[i] for i in indices], list(indices), policy, padding)
        assert rebuilt == data


def test_replication_encodes_independent_copies():
    policy = validate_policy({})
    shares, padding = encode(b"the same chunk", policy)
    assert shares == [b"the same chunk"] * 3 and padding == 0
    assert decode(shares[1:2], [1], policy, 0) == b"the same chunk"


def test_erasure_configuration_cannot_exceed_available_node_limit():
    with pytest.raises(ValueError):
        validate_policy({"mode": "erasure", "k": 32, "m": 1, "w": 32, "r": 32})
