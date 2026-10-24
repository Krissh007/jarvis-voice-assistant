"""A small QR code maker for the phone's pairing link (byte mode, error correction M, versions 1-10).

Settings -> Phone shows the pairing link as a QR code; no library for it is installed and none is loaded
from outside, so this follows the QR standard (ISO/IEC 18004) directly: data and Reed-Solomon codewords,
the fixed patterns, the zigzag placement and the mask with the lowest penalty. Up to 213 bytes fit.
"""

from __future__ import annotations

# Error correction M, per version: (Reed-Solomon codewords per block, number of blocks).
_ECC_M = {1: (10, 1), 2: (16, 1), 3: (26, 1), 4: (18, 2), 5: (24, 2), 6: (16, 4), 7: (18, 4), 8: (22, 4),
          9: (22, 5), 10: (26, 5)}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
          9: [6, 26, 46], 10: [6, 28, 50]}
_FORMAT_M = 0  # error correction level M's two format bits


def _raw_modules(version: int) -> int:
    """Data and error correction bits a version holds (everything but the fixed patterns)."""
    result = (16 * version + 128) * version + 64
    if version >= 2:
        n = version // 7 + 2
        result -= (25 * n - 10) * n - 55
        if version >= 7:
            result -= 36
    return result


def _data_codewords(version: int) -> int:
    ec, blocks = _ECC_M[version]
    return _raw_modules(version) // 8 - ec * blocks


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _codewords(data: bytes, version: int) -> list[int]:
    """The data in byte mode, padded, split into blocks with their error correction, interleaved."""
    capacity = _data_codewords(version) * 8
    bits: list[int] = []

    def put(value: int, n: int) -> None:
        bits.extend((value >> i) & 1 for i in reversed(range(n)))

    put(0b0100, 4)
    put(len(data), 8 if version < 10 else 16)
    for b in data:
        put(b, 8)
    put(0, min(4, capacity - len(bits)))
    put(0, -len(bits) % 8)
    pad = 0xEC
    while len(bits) < capacity:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]

    ec_len, num_blocks = _ECC_M[version]
    raw = _raw_modules(version) // 8
    short_blocks = num_blocks - raw % num_blocks
    short_len = raw // num_blocks
    divisor = _rs_divisor(ec_len)
    blocks, k = [], 0
    for i in range(num_blocks):
        n = short_len - ec_len + (0 if i < short_blocks else 1)
        part = words[k:k + n]
        k += n
        blocks.append((part, _rs_remainder(part, divisor)))
    result = []
    for i in range(max(len(p) for p, _ in blocks)):
        result.extend(p[i] for p, _ in blocks if i < len(p))
    for i in range(ec_len):
        result.extend(e[i] for _, e in blocks)
    return result


_MASKS = [
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
]


class _Grid:
    def __init__(self, version: int) -> None:
        self.version = version
        self.size = version * 4 + 17
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.fixed = [[False] * self.size for _ in range(self.size)]

    def set_fixed(self, x: int, y: int, dark: bool) -> None:
        self.dark[y][x] = dark
        self.fixed[y][x] = True

    def draw_patterns(self) -> None:
        size = self.size
        for i in range(size):  # timing patterns
            self.set_fixed(6, i, i % 2 == 0)
            self.set_fixed(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):  # finders and their separators
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < size and 0 <= y < size:
                        self.set_fixed(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = _ALIGN[self.version]
        last = len(pos) - 1
        for i, ay in enumerate(pos):
            for j, ax in enumerate(pos):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set_fixed(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format(0)  # reserves the format areas; drawn again with the chosen mask
        if self.version >= 7:
            rem = self.version
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.version << 12 | rem
            for i in range(18):
                bit = (bits >> i) & 1 == 1
                a, b = size - 11 + i % 3, i // 3
                self.set_fixed(a, b, bit)
                self.set_fixed(b, a, bit)

    def draw_format(self, mask: int) -> None:
        data = _FORMAT_M << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = lambda i: (bits >> i) & 1 == 1  # noqa: E731
        size = self.size
        for i in range(6):
            self.set_fixed(8, i, bit(i))
        self.set_fixed(8, 7, bit(6))
        self.set_fixed(8, 8, bit(7))
        self.set_fixed(7, 8, bit(8))
        for i in range(9, 15):
            self.set_fixed(14 - i, 8, bit(i))
        for i in range(8):
            self.set_fixed(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set_fixed(8, size - 15 + i, bit(i))
        self.set_fixed(8, size - 8, True)  # always dark

    def place(self, words: list[int]) -> None:
        size, i, total = self.size, 0, len(words) * 8
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(size):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = size - 1 - vert if upward else vert
                    if not self.fixed[y][x] and i < total:
                        self.dark[y][x] = (words[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def apply_mask(self, mask: int) -> None:
        test = _MASKS[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.fixed[y][x] and test(x, y):
                    self.dark[y][x] = not self.dark[y][x]

    def penalty(self) -> int:
        size, score = self.size, 0
        lines = ["".join("1" if d else "0" for d in row) for row in self.dark]
        lines += ["".join("1" if self.dark[y][x] else "0" for y in range(size)) for x in range(size)]
        for line in lines:  # runs of five or more, and finder-like patterns
            run, prev = 0, None
            for ch in line:
                if ch == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run, prev = 1, ch
            if run >= 5:
                score += run - 2
            padded = "0000" + line + "0000"
            for pattern in ("00001011101", "10111010000"):
                start = padded.find(pattern)
                while start != -1:
                    score += 40
                    start = padded.find(pattern, start + 1)
        for y in range(size - 1):  # 2x2 blocks of one colour
            for x in range(size - 1):
                c = self.dark[y][x]
                if c == self.dark[y][x + 1] == self.dark[y + 1][x] == self.dark[y + 1][x + 1]:
                    score += 3
        darks = sum(map(sum, self.dark))
        score += (abs(darks * 20 - size * size * 10) + size * size - 1) // (size * size) * 10
        return score


def matrix(text: str, mask: int | None = None) -> list[list[bool]]:
    """The QR code for text as rows of dark (True) and light modules, without the quiet zone."""
    data = text.encode("utf-8")
    version = next((v for v in _ECC_M if 4 + (8 if v < 10 else 16) + 8 * len(data) <= _data_codewords(v) * 8), None)
    if version is None:
        raise ValueError("too long for a QR code here (213 bytes at most)")
    words = _codewords(data, version)
    best = None
    for m in range(8) if mask is None else [mask]:
        grid = _Grid(version)
        grid.draw_patterns()
        grid.place(words)
        grid.apply_mask(m)
        grid.draw_format(m)
        score = grid.penalty()
        if best is None or score < best[0]:
            best = (score, grid)
    return best[1].dark


def svg(text: str, quiet: int = 4) -> str:
    """The QR code as a small SVG (dark modules on white, with the standard quiet zone)."""
    rows = matrix(text)
    n = len(rows) + 2 * quiet
    path = "".join(f"M{x + quiet},{y + quiet}h1v1h-1z" for y, row in enumerate(rows) for x, d in enumerate(row) if d)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges">'
            f'<rect width="{n}" height="{n}" fill="#fff"/><path d="{path}" fill="#000"/></svg>')
