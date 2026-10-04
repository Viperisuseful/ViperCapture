"""Update an installed vipercapture command.

The standard library is enough on Linux, macOS, and Windows. Nothing here
calls sudo or a distro package manager. A git checkout is left alone; this
replaces only the installed app directory and keeps its .venv.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import socket
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

DEFAULT_REPO = "Viperisuseful/ViperCapture"
DEFAULT_REF = "master"
MAX_ARCHIVE_BYTES = 80 * 1024 * 1024
_EXCLUDE = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache"}
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_REF = re.compile(r"[A-Za-z0-9._/-]{1,128}\Z")
_REPO = re.compile(r"[A-Za-z0-9_.-]{1,80}/[A-Za-z0-9_.-]{1,100}\Z")
_PATH_META = re.compile(r"[\\\"$`]")

Fetch = Callable[[str], bytes]


class UpdateError(Exception):
    """The install tree was left unchanged, or the check could not finish."""


class _HttpsRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def validate_https_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise UpdateError(f"Refusing non-HTTPS URL: {url}")
    return url


def validate_ref(ref: str) -> str:
    if not _REF.fullmatch(ref) or ref.startswith("/") or ".." in ref.split("/"):
        raise UpdateError(f"Invalid VIPERCAPTURE_REF: {ref}")
    return ref


def validate_repo(repo: str) -> str:
    if not _REPO.fullmatch(repo):
        raise UpdateError(f"Invalid VIPERCAPTURE_GITHUB_REPO: {repo}")
    return repo


def commit_api_url(repo: str, ref: str) -> str:
    quoted = urllib.parse.quote(ref, safe="")
    return f"https://api.github.com/repos/{repo}/commits/{quoted}"


def default_archive_url(repo: str, ref: str) -> str:
    quoted = urllib.parse.quote(ref, safe="/")
    return f"https://github.com/{repo}/archive/refs/heads/{quoted}.tar.gz"


def commit_archive_url(repo: str, sha: str) -> str:
    """Download the exact commit that was just checked, not a branch that can move."""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise UpdateError("Refusing to download an archive for an invalid revision.")
    return f"https://github.com/{repo}/archive/{sha}.tar.gz"


def commit_sha_from_api(payload: bytes) -> str:
    try:
        data = json.loads(payload.decode("utf-8"))
        sha = data["sha"]
    except (UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise UpdateError("GitHub did not return a commit SHA.") from exc
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        raise UpdateError("GitHub did not return a commit SHA.")
    return sha.lower()


def consume_limited(read: Callable[[int], bytes], limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        block = read(64 * 1024)
        if not block:
            break
        total += len(block)
        if total > limit:
            raise UpdateError("The download is larger than 80 MB.")
        chunks.append(block)
    return b"".join(chunks)


def fetch_bytes(url: str, *, timeout: float = 60, limit: int = MAX_ARCHIVE_BYTES) -> bytes:
    validate_https_url(url)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ViperCapture",
            "Accept": "application/vnd.github+json, application/octet-stream",
        },
    )
    opener = urllib.request.build_opener(_HttpsRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return consume_limited(response.read, limit)
    except UpdateError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"Could not download {url}.") from exc


def read_revision(prefix: Path) -> str | None:
    path = prefix / "revision"
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip().lower()
    except OSError:
        return None
    if _SHA.fullmatch(text):
        return text
    return None


def write_revision(prefix: Path, sha: str) -> None:
    if not _SHA.fullmatch(sha):
        raise UpdateError("Refusing to record an invalid revision.")
    path = prefix / "revision"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    if hasattr(os, "fchmod"):
        os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(sha + "\n")


def read_version(app: Path) -> str:
    try:
        return (app / "VERSION").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract a GitHub archive without following links or absolute paths."""
    root = dest.resolve()
    for member in tar.getmembers():
        name = member.name.replace("\\", "/")
        parts = Path(name).parts
        target = (dest / name).resolve()
        if (
            not name
            or name.startswith("/")
            or ".." in parts
            or not _within(target, root)
        ):
            raise UpdateError("The archive contains an unsafe path.")
        if member.issym() or member.islnk():
            continue
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not member.isreg():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        source = tar.extractfile(member)
        if source is None:
            continue
        with source, target.open("wb") as handle:
            shutil.copyfileobj(source, handle)
        mode = member.mode & 0o777
        if mode:
            target.chmod(mode)


def extract_archive(payload: bytes, dest: Path) -> Path:
    try:
        tar = tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz")
    except tarfile.TarError as exc:
        raise UpdateError("The download is not a gzip tar archive.") from exc
    with tar:
        safe_extract(tar, dest)
    if (dest / "launch.py").is_file():
        return dest
    for child in dest.iterdir():
        if child.is_dir() and (child / "launch.py").is_file():
            return child
    raise UpdateError("The downloaded archive does not include launch.py.")


def copy_filtered(source: Path, dest: Path) -> None:
    """Copy source into dest, skipping virtualenvs, VCS metadata, and links."""
    dest.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        if entry.name in _EXCLUDE or entry.is_symlink():
            continue
        target = dest / entry.name
        if entry.is_dir():
            copy_filtered(entry, target)
            if not any(target.iterdir()):
                target.rmdir()
        elif entry.is_file():
            shutil.copy2(entry, target)


# Machine-only files. The GitHub archive does not contain them.
_LOCAL_FILES = (".env.local",)


def _preserve_local_files(backup: Path, app: Path) -> None:
    """Copy local config into the new tree. Symlinks are left behind."""
    if not backup.is_dir() or not app.is_dir():
        return
    for name in _LOCAL_FILES:
        source = backup / name
        if not source.exists() or source.is_symlink() or not source.is_file():
            continue
        target = app / name
        if target.is_symlink():
            target.unlink()
        elif target.is_dir():
            continue
        shutil.copy2(source, target)


def _recover_interrupted_update(app: Path, backup: Path) -> None:
    """Put .venv and local config back if an earlier update stopped halfway."""
    if not backup.exists():
        return
    if not app.exists():
        backup.rename(app)
        return
    stray = backup / ".venv"
    if stray.exists() and not (app / ".venv").exists():
        stray.rename(app / ".venv")
    _preserve_local_files(backup, app)
    shutil.rmtree(backup)


def replace_install_tree(source: Path, app: Path) -> None:
    """Swap in a new app tree and move the existing .venv across."""
    if not (source / "launch.py").is_file():
        raise UpdateError("The downloaded archive does not include launch.py.")
    parent = app.parent
    staging = parent / "app.updating"
    backup = parent / "app.backup"
    if staging.exists():
        shutil.rmtree(staging)
    _recover_interrupted_update(app, backup)
    copy_filtered(source, staging)
    if not (staging / "launch.py").is_file():
        shutil.rmtree(staging)
        raise UpdateError("The downloaded archive does not include launch.py.")
    try:
        if app.exists():
            app.rename(backup)
        staging.rename(app)
    except OSError as exc:
        if backup.exists() and not app.exists():
            backup.rename(app)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError(
            "Could not replace the installed files. Stop vipercapture and run update again."
        ) from exc
    venv = backup / ".venv" if backup.exists() else None
    if venv is not None and venv.exists():
        try:
            venv.rename(app / ".venv")
        except OSError as exc:
            shutil.rmtree(app, ignore_errors=True)
            if not app.exists() and backup.exists():
                backup.rename(app)
            raise UpdateError("Could not keep the existing virtualenv.") from exc
    try:
        _preserve_local_files(backup, app)
    except OSError as exc:
        raise UpdateError("Could not keep .env.local.") from exc
    if backup.exists():
        shutil.rmtree(backup)


def shell_single_quote(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise UpdateError("The Python path contains a newline.")
    return "'" + value.replace("'", "'\\''") + "'"


def cmd_quote(value: str) -> str:
    if "\n" in value or "\r" in value or "%" in value:
        raise UpdateError("The command path cannot be quoted for cmd.exe.")
    return '"' + value.replace('"', '""') + '"'


def cmd_encoding() -> str:
    """The code page cmd.exe uses for a batch file that has no UTF-8 BOM."""
    if sys.platform != "win32":
        return "ascii"
    import ctypes

    code_page = int(ctypes.windll.kernel32.GetOEMCP())
    return f"cp{code_page}"


def write_shim(
    path: Path,
    python: str,
    launch: Path,
    platform_name: str,
    *,
    encoding: str | None = None,
) -> bool:
    """Write the user command. Return True when the file is new."""
    created = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if platform_name == "win32":
        text = f"@echo off\r\n{cmd_quote(python)} {cmd_quote(str(launch))} %*\r\n"
        chosen = encoding or cmd_encoding()
        try:
            data = text.encode(chosen)
        except LookupError as exc:
            raise UpdateError("This Windows install cannot encode the vipercapture command.") from exc
        except UnicodeEncodeError as exc:
            raise UpdateError(
                "The Windows command path cannot be stored in the console code page. "
                "Move the install to an ASCII path and run vipercapture update again."
            ) from exc
        path.write_bytes(data)
    else:
        quoted = f"{shell_single_quote(python)} {shell_single_quote(str(launch))}"
        text = f"#!/bin/sh\nexec {quoted} \"$@\"\n"
        path.write_text(text, encoding="utf-8", newline="")
        path.chmod(0o755)
    return created


def read_shim_text(path: Path, platform_name: str) -> str:
    data = path.read_bytes()
    if platform_name == "win32":
        for encoding in (cmd_encoding(), "utf-8"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
    return data.decode("utf-8", errors="replace")


def shims_for_install(
    app: Path,
    *,
    platform_name: str,
    path_env: str,
    home: Path,
) -> list[Path]:
    launch = str(app / "launch.py")
    names = ("vipercapture.cmd", "vipercapture") if platform_name == "win32" else ("vipercapture",)
    found: list[Path] = []
    seen: set[Path] = set()
    for directory in path_env.split(os.pathsep):
        if not directory:
            continue
        for name in names:
            candidate = Path(directory) / name
            try:
                key = candidate.resolve()
            except OSError:
                key = candidate
            if key in seen or not candidate.is_file():
                continue
            try:
                text = read_shim_text(candidate, platform_name)
            except OSError:
                continue
            if launch in text:
                seen.add(key)
                found.append(candidate)
    if found:
        return found
    if platform_name == "win32":
        return [app.parent / "bin" / "vipercapture.cmd"]
    return [home / ".local" / "bin" / "vipercapture"]


def shim_python(executable: str, app: Path) -> str:
    venv = app / ".venv"
    try:
        if Path(executable).resolve().is_relative_to(venv.resolve()):
            raise UpdateError("Refusing to point vipercapture at the virtualenv Python.")
    except OSError:
        pass
    return executable


def escape_posix_path(value: str) -> str:
    return _PATH_META.sub(lambda match: "\\" + match.group(0), value)


def posix_rc_file(home: Path, shell: str) -> Path:
    name = Path(shell).name if shell else ""
    if name == "zsh":
        return home / ".zshrc"
    if name == "bash":
        return home / ".bashrc"
    return home / ".profile"


def ensure_posix_path(bin_dir: Path, home: Path, shell: str) -> str:
    """Record bin_dir in the shell rc file. Return added, present, or skipped."""
    text = str(bin_dir)
    if "\n" in text or "\r" in text:
        return "skipped"
    rc = posix_rc_file(home, shell)
    rc.parent.mkdir(parents=True, exist_ok=True)
    existing = rc.read_text(encoding="utf-8", errors="replace") if rc.exists() else ""
    if "# ViperCapture" in existing:
        return "present"
    line = f'\n# ViperCapture\nexport PATH="{escape_posix_path(text)}:$PATH"\n'
    with rc.open("a", encoding="utf-8") as handle:
        handle.write(line)
    return "added"


def merge_windows_path(current: str, bin_dir: str) -> str | None:
    """Return the user Path value to store, or None when bin_dir is already there."""
    norm = bin_dir.rstrip("\\/").casefold()
    parts = [part for part in current.split(";") if part]
    for part in parts:
        if part.rstrip("\\/").casefold() == norm:
            return None
    if not parts:
        return bin_dir
    return bin_dir + ";" + ";".join(parts)


def ensure_windows_path(bin_dir: Path) -> str:
    import winreg

    access = winreg.KEY_READ | winreg.KEY_SET_VALUE
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, access) as key:
        try:
            current, value_type = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current, value_type = "", winreg.REG_EXPAND_SZ
        if not isinstance(current, str):
            current = str(current)
        updated = merge_windows_path(current, str(bin_dir))
        if updated is None:
            return "present"
        winreg.SetValueEx(key, "Path", 0, value_type, updated)
    return "added"


def server_is_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=0.2):
            return True
    except OSError:
        return False


def _install_label(version: str) -> str:
    if version:
        return f"ViperCapture {version}"
    return "ViperCapture"


def _version_phrase(old: str, new: str) -> str:
    if old and new and old != new:
        return f"from {old} to {new}"
    if new:
        return new
    if old:
        return old
    return "the installed copy"


def run_update(
    root: Path,
    *,
    fetch: Fetch = fetch_bytes,
    repo: str | None = None,
    ref: str | None = None,
    archive_url: str | None = None,
    platform_name: str | None = None,
    home: Path | None = None,
    path_env: str | None = None,
    executable: str | None = None,
    shell: str | None = None,
    server_running: Callable[[], bool] | None = None,
    check_only: bool = False,
) -> int:
    """Refresh the install that contains root. Return a process exit code."""
    try:
        return _run_update(
            root,
            fetch=fetch,
            repo=repo,
            ref=ref,
            archive_url=archive_url,
            platform_name=platform_name,
            home=home,
            path_env=path_env,
            executable=executable,
            shell=shell,
            server_running=server_running,
            check_only=check_only,
        )
    except UpdateError as exc:
        print(f"  {exc}", file=sys.stderr)
        return 1


def _run_update(
    root: Path,
    *,
    fetch: Fetch,
    repo: str | None,
    ref: str | None,
    archive_url: str | None,
    platform_name: str | None,
    home: Path | None,
    path_env: str | None,
    executable: str | None,
    shell: str | None,
    server_running: Callable[[], bool] | None,
    check_only: bool,
) -> int:
    root = root.resolve()
    if (root / ".git").exists():
        raise UpdateError(
            "This copy is a source checkout. vipercapture update refreshes the "
            "installed command, not this directory."
        )
    if not (root / "launch.py").is_file():
        raise UpdateError("This directory does not look like a ViperCapture install.")

    repo = validate_repo(repo or os.environ.get("VIPERCAPTURE_GITHUB_REPO", DEFAULT_REPO))
    ref = validate_ref(ref or os.environ.get("VIPERCAPTURE_REF", DEFAULT_REF))
    if archive_url is None:
        archive_url = os.environ.get("VIPERCAPTURE_ARCHIVE_URL") or ""
    if archive_url:
        validate_https_url(archive_url)
    if platform_name is None:
        platform_name = sys.platform
    if home is None:
        home = Path.home()
    if path_env is None:
        path_env = os.environ.get("PATH", "")
    if executable is None:
        executable = sys.executable
    if shell is None:
        shell = os.environ.get("SHELL", "")
    if server_running is None:
        server_running = server_is_running

    prefix = root.parent
    local_sha = read_revision(prefix)
    local_version = read_version(root)
    remote_sha: str | None = None
    try:
        remote_sha = commit_sha_from_api(fetch(commit_api_url(repo, ref)))
    except UpdateError:
        if check_only:
            raise UpdateError("Could not reach GitHub to check for an update.") from None
        remote_sha = None

    current = bool(remote_sha and local_sha == remote_sha and not archive_url)
    label = _install_label(local_version)
    if check_only:
        if current:
            print(f"  {label} is already up to date.")
            return 0
        print(f"  An update is available for {label}.")
        return 10
    if current:
        print(f"  {label} is already up to date.")
        return 0

    # Windows cannot rename the install tree while its Python is still running.
    if platform_name == "win32" and server_running():
        raise UpdateError(
            "ViperCapture is still running on http://127.0.0.1:8000. "
            "Stop it and run vipercapture update again."
        )

    if archive_url:
        download_url = archive_url
    elif remote_sha:
        download_url = commit_archive_url(repo, remote_sha)
    else:
        download_url = default_archive_url(repo, ref)
    payload = fetch(download_url)
    with tempfile.TemporaryDirectory(prefix="vipercapture-update-") as tmp:
        source = extract_archive(payload, Path(tmp))
        replace_install_tree(source, root)
    new_version = read_version(root)
    python = shim_python(executable, root)
    created_path: Path | None = None
    for shim in shims_for_install(
        root, platform_name=platform_name, path_env=path_env, home=home
    ):
        if write_shim(shim, python, root / "launch.py", platform_name) and created_path is None:
            created_path = shim
    if remote_sha:
        write_revision(prefix, remote_sha)

    print(f"  Updated ViperCapture {_version_phrase(local_version, new_version)}.")
    if server_running():
        print("  A server is still running on http://127.0.0.1:8000.")
        print("  Stop it and start vipercapture again to use this update.")
    if created_path is not None:
        if platform_name == "win32":
            path_status = ensure_windows_path(created_path.parent)
        else:
            path_status = ensure_posix_path(created_path.parent, home, shell)
        if path_status == "added":
            print(f"  Added {created_path.parent} to PATH.")
            print("  Open a new terminal so vipercapture is on PATH.")
    return 0
