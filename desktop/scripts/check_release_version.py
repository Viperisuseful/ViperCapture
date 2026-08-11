"""Reject release tags that do not match the shared Tauri app version."""

import json
import os
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: check_release_version.py <desktop|android>")

    app = sys.argv[1]
    tag = os.environ.get("RELEASE_TAG", "")
    if app not in {"desktop", "android"}:
        raise SystemExit(f"unsupported app: {app}")

    config = Path(__file__).parents[1] / "src-tauri" / "tauri.conf.json"
    version = json.loads(config.read_text(encoding="utf-8"))["version"]
    expected = f"{app}-v{version}"
    if tag != expected:
        raise SystemExit(f"release tag {tag!r} must be {expected!r}")

    print(f"release tag matches app version {version}")


if __name__ == "__main__":
    main()
