#!/usr/bin/env python3
"""Build deterministic source, action, skill, integration, and Go bundles."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "release" / "versions.json"


def source_identity() -> tuple[str, str]:
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    epoch_text = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch_text is None:
        epoch_text = subprocess.check_output(
            ["git", "show", "-s", "--format=%ct", "HEAD"], cwd=ROOT, text=True
        ).strip()
    try:
        epoch = int(epoch_text)
    except ValueError as exc:
        raise SystemExit("SOURCE_DATE_EPOCH must be an integer") from exc
    if epoch < 0:
        raise SystemExit("SOURCE_DATE_EPOCH must not be negative")
    generated_at = datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    return commit, generated_at


def tracked_files(*prefixes: str) -> list[Path]:
    command = ["git", "ls-files", "--", *prefixes]
    output = subprocess.check_output(command, cwd=ROOT, text=True)
    return [ROOT / line for line in output.splitlines() if line]


def zip_bundle(target: Path, files: list[Path], *, prefix: str = "") -> None:
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(files):
            relative = path.relative_to(ROOT).as_posix()
            name = str(PurePosixPath(prefix) / relative) if prefix else relative
            information = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            information.compress_type = zipfile.ZIP_DEFLATED
            information.external_attr = (0o755 if path.suffix in {".sh", ".py"} else 0o644) << 16
            archive.writestr(information, path.read_bytes())


def source_archive(target: Path, version: str) -> None:
    archive = subprocess.check_output(
        ["git", "archive", f"--prefix=ViperCapture-{version}/", "HEAD"], cwd=ROOT
    )
    with target.open("wb") as output:
        import gzip

        with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
            compressed.write(archive)


def go_bundle(target: Path, version: str) -> None:
    files = tracked_files("sdk/go")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(files):
            name = PurePosixPath(f"vipercapture-go-{version}") / path.relative_to(ROOT).as_posix()
            info = archive.gettarinfo(str(path), arcname=str(name))
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            with path.open("rb") as source:
                archive.addfile(info, source)
    import gzip

    with target.open("wb") as output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
            compressed.write(buffer.getvalue())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    versions = json.loads(VERSIONS.read_text("utf-8"))
    source_commit, generated_at = source_identity()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / "python").mkdir(exist_ok=True)
    (output / "typescript").mkdir(exist_ok=True)
    version = versions["oss"]

    source_archive(output / f"ViperCapture-{version}.tar.gz", version)
    zip_bundle(
        output / f"vipercapture-action-{version}.zip",
        tracked_files("action.yml", "skills/vipercapture"),
    )
    zip_bundle(
        output / f"vipercapture-skill-{version}.zip",
        tracked_files("skills/vipercapture"),
    )
    zip_bundle(
        output / f"vipercapture-integrations-{version}.zip",
        tracked_files("integrations"),
    )
    go_bundle(output / f"vipercapture-go-{version}.tar.gz", version)
    manifest = {
        "schema_version": 1,
        "generated_at": generated_at,
        "source_commit": source_commit,
        "versions": versions,
        "destinations": {
            "source_action_skill_integrations": "GitHub Releases",
            "container": "GitHub Container Registry",
            "python_sdk": "PyPI",
            "typescript_sdk": "npm",
            "go_sdk": "GitHub module source",
        },
    }
    (output / "release-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", "utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
