#!/usr/bin/env python3
"""Write stable SHA-256 checksums for every release artifact."""

from __future__ import annotations

import argparse
from hashlib import sha256
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    root = args.directory.resolve()
    target = root / "SHA256SUMS.txt"
    lines = []
    for path in sorted(item for item in root.rglob("*") if item.is_file() and item != target):
        lines.append(f"{sha256(path.read_bytes()).hexdigest()}  {path.relative_to(root).as_posix()}")
    target.write_text("\n".join(lines) + "\n", "ascii")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
