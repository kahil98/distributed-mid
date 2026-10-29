"""Selectable replication/Quorum and systematic Reed-Solomon (zfec) policies."""
import hashlib


class ReedSolomon:
    """Systematic Reed-Solomon over GF(2^8), for at most 32 fragments."""
    _exp, _log = [0] * 512, [0] * 256
    _x = 1
    for _i in range(255):
        _exp[_i] = _x
        _log[_x] = _i
        _x <<= 1
        if _x & 0x100:
            _x ^= 0x11D
    for _i in range(255, 512):
        _exp[_i] = _exp[_i - 255]

    @classmethod
    def mul(cls, a, b):
        return 0 if not a or not b else cls._exp[cls._log[a] + cls._log[b]]

    @classmethod
    def inv(cls, a):
        if not a:
            raise ValueError("singular matrix")
        return cls._exp[255 - cls._log[a]]

    @classmethod
    def invert(cls, matrix):
        n = len(matrix)
        work = [list(row) + [int(i == j) for j in range(n)] for i, row in enumerate(matrix)]
        for col in range(n):
            pivot = next((r for r in range(col, n) if work[r][col]), None)
            if pivot is None:
                raise ValueError("singular matrix")
            work[col], work[pivot] = work[pivot], work[col]
            scale = cls.inv(work[col][col])
            work[col] = [cls.mul(x, scale) for x in work[col]]
            for row in range(n):
                if row != col and work[row][col]:
                    factor = work[row][col]
                    work[row] = [a ^ cls.mul(factor, b) for a, b in zip(work[row], work[col])]
        return [row[n:] for row in work]

    @classmethod
    def generator(cls, k, n):
        vandermonde = [[1 if col == 0 else 0 for col in range(k)] for _ in range(k)]
        for row in range(k):
            for col in range(k):
                vandermonde[row][col] = cls._exp[(row * col) % 255] if col else 1
        top_inv = cls.invert(vandermonde)
        output = []
        for row in range(n):
            powers = [cls._exp[(row * col) % 255] if col else 1 for col in range(k)]
            output.append([_xor_mul_sum(powers, [top_inv[i][col] for i in range(k)], cls)
                           for col in range(k)])
        return output

    @classmethod
    def encode(cls, data, k, n):
        size = (len(data) + k - 1) // k
        size = max(size, 1)
        padded = data + bytes(size * k - len(data))
        originals = [padded[i*size:(i+1)*size] for i in range(k)]
        matrix = cls.generator(k, n)
        return [_combine(row, originals, size, cls) for row in matrix], size * k - len(data)

    @classmethod
    def decode(cls, shares, indices, k, n, padlen):
        if len(indices) < k:
            raise ValueError(f"need {k} healthy shares, have {len(indices)}")
        size = len(shares[0])
        matrix = cls.generator(k, n)
        decode_matrix = cls.invert([matrix[i] for i in indices[:k]])
        data_shares = [_combine(row, shares[:k], size, cls) for row in decode_matrix]
        data = b"".join(data_shares)
        return data[:-padlen] if padlen else data


def _xor_mul_sum(coeffs, values, field):
    result = 0
    for a, b in zip(coeffs, values):
        result ^= field.mul(a, b)
    return result


def _combine(coefficients, shares, size, field):
    result = bytearray(size)
    for coefficient, share in zip(coefficients, shares):
        if coefficient == 1:
            for i, value in enumerate(share):
                result[i] ^= value
        elif coefficient:
            for i, value in enumerate(share):
                result[i] ^= field.mul(coefficient, value)
    return bytes(result)


def validate_policy(policy):
    mode = policy.get("mode", "replication")
    try:
        n, w, r = int(policy.get("n", 3)), int(policy.get("w", 2)), int(policy.get("r", 2))
    except (TypeError, ValueError):
        raise ValueError("n, w, and r must be integers")
    if mode == "replication":
        if not (1 <= w <= n <= 32 and 1 <= r <= n and w + r > n):
            raise ValueError("Replication requires 1 <= W,R <= N <= 32 and W + R > N")
        return {"mode": mode, "n": n, "w": w, "r": r, "k": 1, "m": n}
    if mode == "erasure":
        try:
            k, m = int(policy.get("k", 4)), int(policy.get("m", 2))
        except (TypeError, ValueError):
            raise ValueError("k and m must be integers")
        n = k + m
        if not (1 <= k < n <= 32 and w >= k and r >= k and w <= n and r <= n and w + r > n):
            raise ValueError("Erasure coding requires 1 <= k < k+m <= 32, W,R >= k, and W + R > k+m")
        return {"mode": mode, "n": n, "w": w, "r": r, "k": k, "m": m}
    raise ValueError("mode must be replication or erasure")


def encode(data, policy):
    if policy["mode"] == "replication":
        return [data] * policy["n"], 0
    return ReedSolomon.encode(data, policy["k"], policy["n"])


def decode(shares, indices, policy, padlen):
    if policy["mode"] == "replication":
        return shares[0]
    return ReedSolomon.decode(shares, indices, policy["k"], policy["n"], padlen)


def fragment_digest(data):
    return hashlib.sha256(data).hexdigest()
