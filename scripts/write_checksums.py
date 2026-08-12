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
    asset_paths: dict[str, Path] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file() and item != target):
        asset_name = path.name
        if asset_name in asset_paths:
            raise SystemExit(
                "duplicate release asset name after GitHub flattening: "
                f"{asset_paths[asset_name].relative_to(root)} and {path.relative_to(root)}"
            )
        asset_paths[asset_name] = path
        lines.append(f"{sha256(path.read_bytes()).hexdigest()}  {asset_name}")
    target.write_text("\n".join(lines) + "\n", "ascii")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
