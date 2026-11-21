"""Gera o ícone do app (CorteX9.icns) com sips/iconutil do macOS."""
import subprocess
import sys
import tempfile
import zlib
import struct
from pathlib import Path


def png(size, path):
    """PNG simples: quadrado arredondado âmbar com um 'X' escuro."""
    r = size * 0.22
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            cx, cy = min(x, size - 1 - x), min(y, size - 1 - y)
            inside = not (cx < r and cy < r and (r - cx) ** 2 + (r - cy) ** 2 > r * r)
            if not inside:
                row += bytes([0, 0, 0, 0])
                continue
            u, v = x / size, y / size
            d1, d2 = abs(u - v), abs(u + v - 1)
            on_x = 0.25 < u < 0.75 and 0.25 < v < 0.75 and (d1 < 0.07 or d2 < 0.07)
            row += bytes([20, 18, 12, 255]) if on_x else bytes([255, 176, 32, 255])
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def main(dest):
    tmp = Path(tempfile.mkdtemp()) / "CorteX9.iconset"
    tmp.mkdir()
    for s in (16, 32, 128, 256, 512):
        png(s, tmp / f"icon_{s}x{s}.png")
        png(s * 2, tmp / f"icon_{s}x{s}@2x.png")
    subprocess.run(["iconutil", "-c", "icns", str(tmp), "-o", str(Path(dest) / "CorteX9.icns")], check=True)


if __name__ == "__main__":
    main(sys.argv[1])
