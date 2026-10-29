"""Streaming fixed and rolling-buzhash content-defined chunkers."""
from collections.abc import Iterable, Iterator

WINDOW = 64
_TABLE = tuple(int.from_bytes(__import__("hashlib").sha256(bytes([i])).digest()[:8], "big") for i in range(256))


def split_chunks(blocks: Iterable[bytes], mode: str, fixed_size: int = 256 * 1024,
                 minimum: int = 16 * 1024, average: int = 64 * 1024,
                 maximum: int = 256 * 1024) -> Iterator[bytes]:
    """Bounded-memory chunk splitter. 'fixed' is the baseline; 'cdc' is rolling buzhash."""
    if mode not in {"fixed", "cdc"}:
        raise ValueError("mode must be fixed or cdc")
    if fixed_size < 1 or not 1 <= minimum <= average <= maximum:
        raise ValueError("invalid chunk sizes")
    mask = (1 << (average - 1).bit_length()) - 1
    window = bytearray()
    current = bytearray()
    rolling = 0
    for block in blocks:
        for byte in block:
            current.append(byte)
            if mode == "fixed":
                boundary = len(current) >= fixed_size
            else:
                if len(window) == WINDOW:
                    outgoing = window.pop(0)
                else:
                    outgoing = None
                window.append(byte)
                rolling = _rot(rolling, 1) ^ _TABLE[byte]
                if outgoing is not None:
                    rolling ^= _rot(_TABLE[outgoing], WINDOW)
                n = len(current)
                boundary = n >= maximum or (n >= minimum and (rolling & mask) == 0)
            if boundary:
                yield bytes(current)
                current.clear()
    if current:
        yield bytes(current)


def _rot(value, amount):
    amount %= 64
    value &= (1 << 64) - 1
    return ((value << amount) | (value >> (64 - amount))) & ((1 << 64) - 1)
