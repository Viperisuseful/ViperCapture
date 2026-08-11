"""Reject release tags that do not match the shared Tauri app version."""

import json
import os
import sys
import tomllib
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: check_release_version.py <desktop|android>")

    app = sys.argv[1]
    tag = os.environ.get("RELEASE_TAG", "")
    if app not in {"desktop", "android"}:
        raise SystemExit(f"unsupported app: {app}")

    root = Path(__file__).parents[1]
    src_tauri = root / "src-tauri"
    cargo = tomllib.loads((src_tauri / "Cargo.toml").read_text(encoding="utf-8"))
    cargo_lock = tomllib.loads((src_tauri / "Cargo.lock").read_text(encoding="utf-8"))
    locked_app = next(
        package for package in cargo_lock["package"] if package["name"] == "vipercapture-desktop"
    )
    versions = {
        "package.json": json.loads((root / "package.json").read_text(encoding="utf-8"))["version"],
        "package-lock.json": json.loads(
            (root / "package-lock.json").read_text(encoding="utf-8")
        )["version"],
        "Cargo.toml": cargo["package"]["version"],
        "Cargo.lock": locked_app["version"],
        "tauri.conf.json": json.loads(
            (src_tauri / "tauri.conf.json").read_text(encoding="utf-8")
        )["version"],
        "tauri.android.conf.json": json.loads(
            (src_tauri / "tauri.android.conf.json").read_text(encoding="utf-8")
        )["version"],
    }
    version = versions["tauri.conf.json"]
    mismatches = [name for name, candidate in versions.items() if candidate != version]
    if mismatches:
        details = ", ".join(f"{name}={versions[name]}" for name in mismatches)
        raise SystemExit(f"app versions do not match tauri.conf.json={version}: {details}")

    expected = f"{app}-v{version}"
    if tag != expected:
        raise SystemExit(f"release tag {tag!r} must be {expected!r}")

    print(f"release tag matches app version {version}")


if __name__ == "__main__":
    main()
