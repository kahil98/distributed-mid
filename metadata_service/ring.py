"""Deterministic SHA-256 consistent-hash ring with virtual nodes."""
from bisect import bisect
import hashlib


class ConsistentHashRing:
    def __init__(self, nodes=(), virtual_nodes=128):
        if virtual_nodes < 1:
            raise ValueError("virtual_nodes must be positive")
        self.virtual_nodes = virtual_nodes
        self._members = {}
        self._points = []
        self._owners = []
        for node in nodes:
            self.add_node(node)

    @staticmethod
    def _hash(value):
        return int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest(), "big")

    def add_node(self, node):
        node = str(node)
        if not node:
            raise ValueError("node ID must not be empty")
        if node in self._members:
            return
        points = sorted((self._hash(f"{node}:vnode:{i}"), node) for i in range(self.virtual_nodes))
        self._members[node] = points
        combined = sorted(list(self._owner_pairs()) + points)
        self._points = [p for p, _ in combined]
        self._owners = [n for _, n in combined]

    def _owner_pairs(self):
        return zip(self._points, self._owners)

    def remove_node(self, node):
        node = str(node)
        if node not in self._members:
            return False
        del self._members[node]
        pairs = sorted((p, n) for p, n in self._owner_pairs() if n != node)
        self._points = [p for p, _ in pairs]
        self._owners = [n for _, n in pairs]
        return True

    def get_node(self, key):
        if not self._points:
            raise LookupError("cannot place chunks: ring has no nodes")
        position = bisect(self._points, self._hash(str(key))) % len(self._points)
        return self._owners[position]

    @property
    def nodes(self):
        return tuple(sorted(self._members))
