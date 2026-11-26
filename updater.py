"""Update an installed vipercapture command.

The standard library is enough on Linux, macOS, and Windows. Nothing here
calls sudo or a distro package manager. A git checkout is left alone; this
replaces only the installed app directory and keeps its .venv and
.vipercapture data directory.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import platform
import re
import shlex
import shutil
import socket
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable

DEFAULT_REPO = "Viperisuseful/ViperCapture"
DEFAULT_REF = "master"
MAX_ARCHIVE_BYTES = 80 * 1024 * 1024
_EXCLUDE = {
    ".git",
    ".venv",
    ".vipercapture",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
}
# Directories that belong to this machine. .venv is the install virtualenv.
# .vipercapture holds presets and the control-plane database when async jobs
# are off, which is the Windows default.
_KEPT_DIRS = (".venv", ".vipercapture")
_KEPT_LABELS = {
    ".venv": "the existing virtualenv",
    ".vipercapture": "saved presets and local data",
}
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
    try:
        parsed = urllib.parse.urlparse(url)
        _ = parsed.port
    except ValueError:
        raise UpdateError("Refusing invalid URL.") from None
    if not parsed.netloc:
        raise UpdateError("Refusing invalid URL.")
    if parsed.scheme != "https":
        raise UpdateError(f"Refusing non-HTTPS URL: {_display_url(url)}")
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
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        ValueError,
        TimeoutError,
        OSError,
    ) as exc:
        raise UpdateError(f"Could not download {_display_url(url)}.") from exc


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
    """Put local directories and config back if an earlier update stopped halfway."""
    if not backup.exists():
        return
    if not app.exists():
        backup.rename(app)
        return
    for name in _KEPT_DIRS:
        stray = backup / name
        if stray.is_symlink() or not stray.is_dir():
            continue
        if (app / name).exists():
            continue
        stray.rename(app / name)
    _preserve_local_files(backup, app)
    shutil.rmtree(backup)


def _move_kept_dirs(backup: Path, app: Path) -> None:
    """Move machine-local directories from the backup into the new app tree."""
    if not backup.is_dir():
        return
    moved: list[str] = []
    current = ""
    try:
        for name in _KEPT_DIRS:
            current = name
            source = backup / name
            if source.is_symlink() or not source.is_dir():
                continue
            destination = app / name
            if destination.is_symlink():
                destination.unlink()
            elif destination.exists():
                shutil.rmtree(destination)
            source.rename(destination)
            moved.append(name)
    except OSError as exc:
        for name in reversed(moved):
            restored = app / name
            if restored.exists() and not (backup / name).exists():
                restored.rename(backup / name)
        shutil.rmtree(app, ignore_errors=True)
        if not app.exists() and backup.exists():
            backup.rename(app)
        label = _KEPT_LABELS.get(current, "local data")
        raise UpdateError(f"Could not keep {label}.") from exc


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
    _move_kept_dirs(backup, app)
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


_WINDOWS_SHIM_VENV_MARKER = ".venv".encode("utf-16le")


def _find_csc_compiler() -> Path | None:
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    for rel in (r"Microsoft.NET\Framework64", r"Microsoft.NET\Framework"):
        root = Path(system_root) / rel
        if not root.is_dir():
            continue
        candidates = sorted(
            (child / "csc.exe" for child in root.iterdir() if child.is_dir() and child.name.startswith("v4.")),
            key=lambda p: p.parent.name,
            reverse=True,
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate
    return None


def _extract_windows_shim_source(app_root: Path) -> str | None:
    script = app_root / "scripts" / "install.ps1"
    if not script.is_file():
        return None
    try:
        text = script.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(
        r"\$csSource\s*=\s*@'\r?\n(.*?)\r?\n'@",
        text,
        re.DOTALL,
    )
    if not match:
        return None
    source = match.group(1)
    if "class ViperCaptureShim" not in source:
        return None
    return source


def _upgrade_windows_exe_shim(exe_path: Path, app_root: Path) -> bool:
    """Recompile an older vipercapture.exe so it uses the direct .venv fast path."""
    if sys.platform != "win32" or not exe_path.is_file():
        return False
    old_path = exe_path.with_name(exe_path.name + ".old")
    try:
        old_path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        if _WINDOWS_SHIM_VENV_MARKER in exe_path.read_bytes():
            return False
    except OSError:
        return False
    source = _extract_windows_shim_source(app_root)
    csc = _find_csc_compiler()
    if not source or csc is None:
        return False
    import subprocess

    with tempfile.TemporaryDirectory(prefix="vipercapture-shim-") as tmp:
        tmp_dir = Path(tmp)
        cs_path = tmp_dir / "ViperCaptureShim.cs"
        built_exe = tmp_dir / "vipercapture.exe"
        cs_path.write_text(source, encoding="utf-8", newline="\n")
        try:
            result = subprocess.run(
                [str(csc), "/nologo", "/optimize+", "/target:exe", f"/out:{built_exe}", str(cs_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode != 0 or not built_exe.is_file():
            return False
        staged = exe_path.with_name(exe_path.name + ".new")
        try:
            shutil.copy2(built_exe, staged)
            try:
                staged.replace(exe_path)
            except OSError:
                # Windows blocks overwriting a running executable, but allows
                # renaming it aside first so the upgraded binary takes effect.
                exe_path.replace(old_path)
                try:
                    staged.replace(exe_path)
                except OSError:
                    try:
                        if exe_path.exists():
                            exe_path.unlink(missing_ok=True)
                        old_path.replace(exe_path)
                    except OSError:
                        pass
                    if not exe_path.is_file():
                        raise UpdateError(
                            "Could not replace vipercapture.exe. Close running instances and run vipercapture update again."
                        ) from None
                    return False
                try:
                    old_path.unlink(missing_ok=True)
                except OSError:
                    pass
            return True
        except OSError:
            if not exe_path.is_file():
                raise UpdateError(
                    "Could not replace vipercapture.exe. Close running instances and run vipercapture update again."
                ) from None
            return False
        finally:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass


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
        if path.suffix.lower() == ".exe":
            launch_text = str(launch)
            if any(char in python for char in "\r\n") or any(char in launch_text for char in "\r\n"):
                raise UpdateError("The Windows command path cannot contain a newline.")
            config_path = path.with_suffix(".shim")
            temporary = config_path.with_suffix(".shim.tmp")
            try:
                temporary.write_text(f"{python}\n{launch_text}\n", encoding="utf-8", newline="\n")
                temporary.replace(config_path)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
            _upgrade_windows_exe_shim(path, launch.parent)
            return created
        venv_python = PureWindowsPath(str(launch)).parent / ".venv" / "Scripts" / "python.exe"
        text = (
            "@echo off\r\n"
            "setlocal disabledelayedexpansion\r\n"
            f'if /i not "%~1"=="update" if exist {cmd_quote(str(venv_python))} goto :venv\r\n'
            f"{cmd_quote(python)} {cmd_quote(str(launch))} %*\r\n"
            "exit /b %ERRORLEVEL%\r\n"
            ":venv\r\n"
            f"{cmd_quote(str(venv_python))} {cmd_quote(str(launch))} %*\r\n"
            "exit /b %ERRORLEVEL%\r\n"
        )
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
        venv_python = launch.parent / ".venv" / "bin" / "python"
        quoted_venv = f"{shell_single_quote(str(venv_python))} {shell_single_quote(str(launch))}"
        quoted = f"{shell_single_quote(python)} {shell_single_quote(str(launch))}"
        text = (
            "#!/bin/sh\n"
            f'if [ "${{1:-}}" != "update" ] && [ -x {shell_single_quote(str(venv_python))} ]; then\n'
            f'    exec {quoted_venv} "$@"\n'
            "fi\n"
            f'exec {quoted} "$@"\n'
        )
        path.write_text(text, encoding="utf-8", newline="")
        path.chmod(0o755)
    return created


def read_shim_text(path: Path, platform_name: str) -> str:
    if platform_name == "win32" and path.suffix.lower() == ".exe":
        config_path = path.with_suffix(".shim")
        if config_path.is_file():
            return config_path.read_text(encoding="utf-8", errors="replace")
        return ""
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
    names = (
        ("vipercapture.exe", "vipercapture.cmd", "vipercapture")
        if platform_name == "win32"
        else ("vipercapture",)
    )
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
        exe_shim = app.parent / "bin" / "vipercapture.exe"
        if exe_shim.is_file():
            return [exe_shim]
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


_ARCH_ALIASES = {
    "x86_64": "x64",
    "amd64": "x64",
    "x64": "x64",
    "arm64": "arm64",
    "aarch64": "arm64",
    "i386": "x86",
    "i686": "x86",
    "x86": "x86",
    "armv7l": "armv7",
    "armv7": "armv7",
    "armv6l": "armv6",
    "ppc64le": "ppc64le",
    "s390x": "s390x",
    "riscv64": "riscv64",
}
_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_WIN_SHELL_EXES = {
    "powershell.exe": "PowerShell",
    "pwsh.exe": "PowerShell",
    "cmd.exe": "Command Prompt",
    "bash.exe": "bash",
    "zsh.exe": "zsh",
    "fish.exe": "fish",
    "sh.exe": "sh",
    "nu.exe": "nu",
}
_POSIX_SHELL_NAMES = {
    "bash": "bash",
    "zsh": "zsh",
    "fish": "fish",
    "sh": "sh",
    "dash": "dash",
    "ash": "ash",
    "ksh": "ksh",
    "mksh": "mksh",
    "csh": "csh",
    "tcsh": "tcsh",
    "pwsh": "PowerShell",
    "nu": "nu",
    "elvish": "elvish",
    "xonsh": "xonsh",
}
_POSIX_PROFILE_SHELLS = frozenset({"sh", "dash", "ash", "ksh", "mksh"})
_POSIX_SYSTEM_PATH_DIRS = frozenset({"/usr/local/bin", "/usr/bin", "/bin"})
_RC_PATH_ASSIGN_RE = re.compile(
    r"^(?:export\s+|typeset\s+-x\s+|setenv\s+|set\s+(?:-[a-zA-Z]+\s+)*)?(?:PATH|\$env:PATH)\s*(?:=|\s+)",
    re.IGNORECASE,
)
_RC_FISH_ADD_PATH_RE = re.compile(r"^fish_add_path\b")
_RC_CSH_PATH_RE = re.compile(r"^set\s+path\s*=")


def _install_label(version: str) -> str:
    if version:
        return f"ViperCapture {version}"
    return "ViperCapture"


def _short_sha(sha: str | None) -> str:
    return sha[:7] if sha else ""


def detect_platform_label(platform_name: str, machine: str | None = None) -> str:
    lowered = platform_name.lower()
    if lowered in ("win32", "cygwin", "msys"):
        os_name = "Windows"
    elif lowered == "darwin":
        os_name = "macOS"
    elif lowered.startswith("linux"):
        os_name = "Linux"
    elif lowered.startswith("freebsd"):
        os_name = "FreeBSD"
    elif lowered.startswith("openbsd"):
        os_name = "OpenBSD"
    elif lowered.startswith("netbsd"):
        os_name = "NetBSD"
    else:
        os_name = (platform.system() or platform_name) if platform_name == sys.platform else platform_name

    if machine is None:
        if lowered == "win32" and sys.platform == "win32":
            raw_arch = os.environ.get("PROCESSOR_ARCHITEW6432") or platform.machine()
        else:
            raw_arch = platform.machine()
    else:
        raw_arch = machine
    norm_arch = raw_arch.strip().lower()
    arch = _ARCH_ALIASES.get(norm_arch, norm_arch)
    if arch:
        return f"{os_name} ({arch})"
    return os_name


def _windows_ancestor_process_names() -> list[str]:
    """Return executable basenames (lowercased) of ancestor processes on Windows, nearest first."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    th32cs_snapprocess = 0x00000002
    invalid_handle = ctypes.c_void_p(-1).value
    max_path = 260

    class _ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * max_path),
        ]

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        snapshot = kernel32.CreateToolhelp32Snapshot(th32cs_snapprocess, 0)
        if snapshot in (None, 0, invalid_handle):
            return []
        processes: dict[int, tuple[int, str]] = {}
        try:
            entry = _ProcessEntry32W()
            entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
            if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                while True:
                    processes[int(entry.th32ProcessID)] = (
                        int(entry.th32ParentProcessID),
                        str(entry.szExeFile).lower(),
                    )
                    if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                        break
        finally:
            kernel32.CloseHandle(snapshot)
    except OSError:
        return []

    names: list[str] = []
    seen: set[int] = set()
    pid = os.getpid()
    while pid and pid not in seen and len(names) < 16:
        seen.add(pid)
        info = processes.get(pid)
        if info is None:
            break
        parent_pid, _ = info
        parent_info = processes.get(parent_pid)
        if parent_info is None:
            break
        names.append(parent_info[1])
        pid = parent_pid
    return names


def _is_launcher_wrapper(exe_name: str) -> bool:
    if exe_name in ("py.exe", "vipercapture.exe"):
        return True
    return exe_name.startswith("python") and exe_name.endswith(".exe")


def _detect_windows_shell_from_ancestors(
    ancestors: list[str],
    *,
    via_cmd_shim: bool = False,
) -> str | None:
    trimmed = [name.lower() for name in ancestors if name]
    while trimmed and _is_launcher_wrapper(trimmed[0]):
        trimmed.pop(0)
    if (
        via_cmd_shim
        and len(trimmed) >= 2
        and trimmed[0] == "cmd.exe"
        and trimmed[1] in _WIN_SHELL_EXES
        and trimmed[1] != "cmd.exe"
    ):
        # Both `pwsh -> vipercapture.cmd` and `pwsh -> cmd -> vipercapture.cmd`
        # produce this ancestry because cmd.exe executes .cmd files in-process.
        return "terminal"
    for name in trimmed:
        if name in _WIN_SHELL_EXES:
            return _WIN_SHELL_EXES[name]
    return None


def _posix_ancestor_process_names() -> list[str]:
    """Return executable basenames (lowercased) of ancestor processes on POSIX, nearest first."""
    if sys.platform == "win32":
        return []
    names: list[str] = []
    if sys.platform.startswith("linux") and Path("/proc").is_dir():
        pid = os.getpid()
        seen: set[int] = set()
        while pid > 1 and pid not in seen and len(names) < 16:
            seen.add(pid)
            try:
                stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
            except OSError:
                break
            close = stat.rfind(")")
            if close == -1:
                break
            fields = stat[close + 2 :].split()
            if len(fields) < 2:
                break
            try:
                ppid = int(fields[1])
            except ValueError:
                break
            if ppid <= 0 or ppid in seen:
                break
            try:
                comm = Path(f"/proc/{ppid}/comm").read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                break
            if comm:
                names.append(comm.lower())
            pid = ppid
        return names

    import subprocess

    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,comm="],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    processes: dict[int, tuple[int, str]] = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            proc_pid = int(parts[0])
            proc_ppid = int(parts[1])
        except ValueError:
            continue
        comm = Path(parts[2].strip()).name.lstrip("-").lower()
        processes[proc_pid] = (proc_ppid, comm)
    pid = os.getpid()
    seen: set[int] = set()
    while pid > 1 and pid not in seen and len(names) < 16:
        seen.add(pid)
        info = processes.get(pid)
        if info is None:
            break
        ppid, _ = info
        parent_info = processes.get(ppid)
        if parent_info is None:
            break
        if parent_info[1]:
            names.append(parent_info[1])
        pid = ppid
    return names


def _detect_posix_shell_from_ancestors(ancestors: list[str]) -> str | None:
    for raw in ancestors:
        name = Path(raw).name.lstrip("-").lower()
        if not name or name in ("env", "vipercapture") or name.startswith("python"):
            continue
        return _POSIX_SHELL_NAMES.get(name)
    return None


def _parse_shell_name(raw: str) -> str:
    cleaned = raw.strip()
    if not cleaned:
        return ""
    base = re.split(r"[\\/]", cleaned.rstrip("\\/"))[-1].lstrip("-").lower()
    if base.endswith(".exe"):
        base = base[:-4]
    if base in ("powershell", "pwsh"):
        return "PowerShell"
    if base == "cmd":
        return "Command Prompt"
    return base


def _detect_shell_name(
    platform_name: str,
    shell: str | None,
    *,
    via_cmd_shim: bool = False,
) -> str:
    if shell is not None:
        return _parse_shell_name(shell) or "terminal"
    if platform_name == "win32":
        if sys.platform == "win32":
            ancestors = _windows_ancestor_process_names()
            if ancestors:
                detected = _detect_windows_shell_from_ancestors(
                    ancestors,
                    via_cmd_shim=via_cmd_shim,
                )
                return detected or "terminal"
        return "terminal"
    if platform_name == sys.platform:
        ancestors = _posix_ancestor_process_names()
        if ancestors:
            return _detect_posix_shell_from_ancestors(ancestors) or "terminal"
        if os.environ.get("POWERSHELL_DISTRIBUTION_CHANNEL"):
            return "PowerShell"
    return "terminal"


def _normalize_path_entry(entry: str, platform_name: str) -> str:
    if platform_name == "win32":
        cleaned = entry.strip().strip('"')
        if not cleaned:
            return ""
        return str(PureWindowsPath(cleaned)).rstrip("\\/").casefold()
    if not entry:
        return ""
    if sys.platform == "win32" and _WIN_DRIVE_RE.match(entry):
        return str(PureWindowsPath(entry)).rstrip("\\/")
    return str(PurePosixPath(entry)).rstrip("/") or "/"


def _path_entries(path_env: str, bin_text: str, platform_name: str) -> list[str]:
    if platform_name == "win32":
        sep = ";"
    elif sys.platform == "win32" and (";" in path_env or _WIN_DRIVE_RE.match(bin_text)):
        sep = ";"
    else:
        sep = ":"
    return [entry for entry in path_env.split(sep) if entry]


def _path_contains(path_env: str, bin_dir: Path, platform_name: str) -> bool:
    bin_text = str(bin_dir)
    target = _normalize_path_entry(bin_text, platform_name)
    if not target:
        return False
    for entry in _path_entries(path_env, bin_text, platform_name):
        if _normalize_path_entry(entry, platform_name) == target:
            return True
    return False


def _windows_pathext(pathext: str | None = None) -> list[str]:
    raw = pathext if pathext is not None else (
        os.environ.get("PATHEXT", "") if sys.platform == "win32" else ".COM;.EXE;.BAT;.CMD"
    )
    exts: list[str] = []
    for part in raw.split(";"):
        ext = part.strip().lower()
        if ext.startswith(".") and ext not in exts:
            exts.append(ext)
    return exts or [".com", ".exe", ".bat", ".cmd"]


def _windows_lookup_names(
    cmd_name: str,
    *,
    shell_name: str,
    pathext: str | None = None,
) -> tuple[str, ...]:
    if cmd_name.lower().endswith(".cmd"):
        return (cmd_name,)
    if shell_name not in ("PowerShell", "Command Prompt", "terminal", ""):
        # POSIX-style shells on Windows (e.g. Git Bash) resolve extensionless and .exe.
        return (cmd_name, f"{cmd_name}.exe")
    exts = _windows_pathext(pathext)
    if shell_name == "Command Prompt":
        return tuple(f"{cmd_name}{ext}" for ext in exts)
    # PowerShell resolves .ps1 ahead of PATHEXT entries in the same directory.
    names = [f"{cmd_name}.ps1"]
    for ext in exts:
        candidate = f"{cmd_name}{ext}"
        if candidate not in names:
            names.append(candidate)
    if shell_name == "terminal" and cmd_name not in names:
        names.append(cmd_name)
    return tuple(names)


def _resolves_to_install_shim(
    path_env: str,
    cmd_name: str,
    install_shims: list[Path],
    platform_name: str,
    *,
    shell_name: str = "",
    pathext: str | None = None,
    cwd: Path | None = None,
) -> bool:
    if not install_shims:
        return False
    expected: set[str] = set()
    for shim in install_shims:
        try:
            resolved = shim.resolve()
        except OSError:
            resolved = shim
        expected.add(_normalize_path_entry(str(resolved), platform_name))
        expected.add(_normalize_path_entry(str(shim), platform_name))
    if platform_name == "win32":
        names = _windows_lookup_names(cmd_name, shell_name=shell_name, pathext=pathext)
        entries = _path_entries(path_env, str(install_shims[0].parent), platform_name)
        if cwd is not None:
            entries = [str(cwd), *entries]
    else:
        names = (cmd_name,)
        if not path_env:
            entries = []
        elif sys.platform == "win32" and (
            ";" in path_env or _WIN_DRIVE_RE.match(str(install_shims[0].parent))
        ):
            entries = [entry for entry in path_env.split(";") if entry]
        else:
            # In POSIX PATH, an empty segment (e.g. `:/opt/bin` or `/opt/bin:`) means cwd.
            posix_cwd = str(cwd) if cwd is not None else "."
            entries = [entry if entry else posix_cwd for entry in path_env.split(":")]
    for entry in entries:
        dir_str = entry.strip().strip('"') if platform_name == "win32" else entry
        if not dir_str:
            continue
        directory = Path(dir_str)
        for name in names:
            candidate = directory / name
            try:
                if not candidate.is_file():
                    continue
                if platform_name != "win32" and platform_name == sys.platform and not os.access(candidate, os.X_OK):
                    continue
                resolved_cand = candidate.resolve()
            except OSError:
                continue
            cand_key = _normalize_path_entry(str(resolved_cand), platform_name)
            raw_key = _normalize_path_entry(str(candidate), platform_name)
            return cand_key in expected or raw_key in expected
    return False


def _read_windows_persistent_path() -> str | None:
    if sys.platform != "win32":
        return None
    import winreg

    parts: list[str] = []
    # Windows composes effective PATH as Machine (HKLM) followed by User (HKCU).
    locations = (
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    )
    read_any = False
    for hive, subkey in locations:
        try:
            with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ) as key:
                read_any = True
                try:
                    value, value_type = winreg.QueryValueEx(key, "Path")
                except FileNotFoundError:
                    continue
                if isinstance(value, str) and value:
                    if value_type == winreg.REG_EXPAND_SZ:
                        value = winreg.ExpandEnvironmentStrings(value)
                    parts.append(value)
        except OSError:
            continue
    if not read_any:
        return None
    return ";".join(parts)


def _bash_login_file(home: Path) -> Path | None:
    for login_file in (home / ".bash_profile", home / ".bash_login", home / ".profile"):
        try:
            if login_file.is_file():
                return login_file
        except OSError:
            continue
    return None


def _split_shell_commands(text: str) -> list[str]:
    """Split a shell line on unquoted `;`, `&&`, and `||` command separators."""
    parts: list[str] = []
    start = 0
    in_single = False
    in_double = False
    escaped = False
    index = 0
    length = len(text)
    while index < length:
        ch = text[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if ch == "\\" and not in_single:
            escaped = True
            index += 1
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if ch == ";":
                parts.append(text[start:index])
                start = index + 1
            elif text[index : index + 2] in ("&&", "||"):
                parts.append(text[start:index])
                start = index + 2
                index += 2
                continue
        index += 1
    parts.append(text[start:])
    return parts


def _parse_shell_word(text: str, home_str: str) -> str:
    """Parse one shell word with quote-aware `$HOME` and leading `~` expansion."""
    buf: list[str] = []
    in_single = False
    in_double = False
    escaped = False
    index = 0
    length = len(text)
    while index < length:
        ch = text[index]
        if escaped:
            buf.append(ch)
            escaped = False
            index += 1
            continue
        if ch == "\\" and not in_single:
            if in_double:
                if index + 1 < length and text[index + 1] in ('\\', '"', "$", "`"):
                    escaped = True
                else:
                    buf.append(ch)
            else:
                escaped = True
            index += 1
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            index += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            index += 1
            continue
        if not in_single and not in_double and ch.isspace():
            break
        if not in_single and not in_double and not buf and ch == "~":
            nxt = text[index + 1 : index + 2]
            if nxt in ("", "/") or nxt.isspace():
                buf.append(home_str)
                index += 1
                continue
        if not in_single:
            if text.startswith("${HOME}", index):
                buf.append(home_str)
                index += 7
                continue
            if text.startswith("$HOME", index):
                nxt = text[index + 5 : index + 6]
                if not nxt or not (nxt.isalnum() or nxt == "_"):
                    buf.append(home_str)
                    index += 5
                    continue
        buf.append(ch)
        index += 1
    return "".join(buf)


def _file_sources_bashrc(path: Path, home: Path) -> bool:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    home_str = str(home).rstrip("/") or "/"
    expected_bashrc = str(home / ".bashrc")
    for raw_line in content.split("\n"):
        code = _strip_shell_comment(raw_line.rstrip("\r")).strip()
        if not code:
            continue
        for clause in _split_shell_commands(code):
            stmt = re.sub(r"^(?:then|else|do)\s+", "", clause.strip())
            match = re.match(r"^(?:source|\.)\s+(.*)$", stmt)
            if not match:
                continue
            operand = _parse_shell_word(match.group(1).lstrip(), home_str)
            if operand == expected_bashrc or operand == f"{home_str}/.bashrc":
                return True
    return False


def _posix_startup_files(home: Path, shell_name: str) -> list[Path]:
    if shell_name == "zsh":
        return [home / ".zshrc", home / ".zshenv", home / ".zprofile"]
    if shell_name == "bash":
        files: list[Path] = []
        login_file = _bash_login_file(home)
        if login_file is None or _file_sources_bashrc(login_file, home):
            files.append(home / ".bashrc")
        if login_file is not None:
            files.append(login_file)
        return files
    if shell_name in _POSIX_PROFILE_SHELLS:
        return [home / ".profile"]
    if shell_name == "fish":
        return [
            home / ".config" / "fish" / "config.fish",
            home / ".config" / "fish" / "fish_variables",
        ]
    if shell_name in ("csh", "tcsh"):
        return [home / ".tcshrc", home / ".cshrc"]
    if shell_name == "PowerShell":
        return [home / ".config" / "powershell" / "Microsoft.PowerShell_profile.ps1"]
    return []


def _shell_reads_posix_rc(shell_name: str, rc_file: Path, home: Path) -> bool:
    if shell_name == "zsh":
        return rc_file.name == ".zshrc"
    if shell_name == "bash":
        return rc_file in _posix_startup_files(home, "bash")
    if shell_name in _POSIX_PROFILE_SHELLS:
        return rc_file.name == ".profile"
    return False


def _strip_shell_comment(line: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    for index, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and not in_single:
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if index == 0 or line[index - 1].isspace():
                return line[:index]
    return line


def _parse_shell_path_entries(rhs: str, home_str: str) -> list[str]:
    """Parse colon-separated PATH entries with quote-aware `$HOME` and `~` expansion."""
    rhs = rhs.strip().rstrip(";").strip()
    entries: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    escaped = False
    index = 0
    length = len(rhs)

    def _flush() -> None:
        value = "".join(buf).rstrip("/") or ("/" if buf else "")
        buf.clear()
        if value:
            entries.append(value)

    while index < length:
        ch = rhs[index]
        if escaped:
            buf.append(ch)
            escaped = False
            index += 1
            continue
        if ch == "\\" and not in_single:
            if in_double:
                if index + 1 < length and rhs[index + 1] in ('\\', '"', "$", "`"):
                    escaped = True
                else:
                    buf.append(ch)
            else:
                escaped = True
            index += 1
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            index += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            index += 1
            continue
        if ch == ":":
            is_drive = (
                sys.platform == "win32"
                and len(buf) == 1
                and buf[0].isalpha()
                and index + 1 < length
                and rhs[index + 1] in ("\\", "/")
            )
            if not is_drive:
                _flush()
                index += 1
                continue
        if not in_single and not in_double and ch.isspace():
            break
        if not in_single and not in_double and not buf and ch == "~":
            nxt = rhs[index + 1 : index + 2]
            if nxt in ("", "/", ":") or nxt.isspace():
                buf.append(home_str)
                index += 1
                continue
        if not in_single:
            if rhs.startswith("${HOME}", index):
                buf.append(home_str)
                index += 7
                continue
            if rhs.startswith("$HOME", index):
                nxt = rhs[index + 5 : index + 6]
                if not nxt or not (nxt.isalnum() or nxt == "_"):
                    buf.append(home_str)
                    index += 5
                    continue
        buf.append(ch)
        index += 1
    _flush()
    return entries


def _rc_line_configures_bin_dir(
    code: str,
    *,
    targets: set[str],
    home_str: str,
) -> bool:
    if _RC_FISH_ADD_PATH_RE.match(code):
        try:
            tokens = shlex.split(code)
        except ValueError:
            tokens = code.split()
        entries = [
            _parse_shell_word(tok, home_str).rstrip("/") or "/"
            for tok in tokens[1:]
            if tok and not tok.startswith("-")
        ]
        return any(entry in targets for entry in entries)
    if _RC_CSH_PATH_RE.match(code):
        match = re.search(r"\((.*)\)", code)
        if not match:
            return False
        try:
            tokens = shlex.split(match.group(1))
        except ValueError:
            tokens = match.group(1).split()
        entries = [_parse_shell_word(tok, home_str).rstrip("/") or "/" for tok in tokens if tok]
        return any(entry in targets for entry in entries)
    if not _RC_PATH_ASSIGN_RE.match(code):
        return False
    # PowerShell profile: $env:PATH = "/dir:" + $env:PATH
    if code.lower().startswith("$env:path"):
        if "=" not in code:
            return False
        _, rhs = code.split("=", 1)
        for seg in rhs.split("+"):
            if any(entry in targets for entry in _parse_shell_path_entries(seg, home_str)):
                return True
        return False
    # fish: set -gx PATH /dir $PATH
    if re.match(r"^set\s+(?:-[a-zA-Z]+\s+)*PATH\s+", code):
        try:
            tokens = shlex.split(code)
        except ValueError:
            tokens = code.split()
        try:
            idx = tokens.index("PATH")
        except ValueError:
            return False
        entries = [
            _parse_shell_word(tok, home_str).rstrip("/") or "/"
            for tok in tokens[idx + 1 :]
            if tok
        ]
        return any(entry in targets for entry in entries)
    # csh/tcsh: setenv PATH "/dir:$PATH"
    if re.match(r"^setenv\s+PATH\s+", code):
        rhs = re.sub(r"^setenv\s+PATH\s+", "", code, count=1)
        return any(entry in targets for entry in _parse_shell_path_entries(rhs, home_str))
    # POSIX: [export ]PATH="/dir:$PATH"
    if "=" in code:
        _, rhs = code.split("=", 1)
        return any(entry in targets for entry in _parse_shell_path_entries(rhs, home_str))
    return False


def _rc_configures_bin_dir(
    content: str,
    *,
    targets: set[str],
    home_str: str,
    is_fish_vars: bool,
) -> bool:
    for raw_line in content.split("\n"):
        stripped = raw_line.rstrip("\r").strip()
        if not stripped or stripped.startswith("#"):
            continue
        if is_fish_vars:
            if not stripped.startswith("SETUVAR fish_user_paths:"):
                continue
            value = stripped.split(":", 1)[1]
            elements = [
                item.replace("\\x20", " ").rstrip("/") or "/"
                for item in re.split(r"(?:\\x1e|\x1e)+", value)
                if item
            ]
            if any(elem in targets for elem in elements):
                return True
            continue
        code = _strip_shell_comment(stripped).strip()
        if not code:
            continue
        for stmt in _split_shell_commands(code):
            stmt = stmt.strip()
            if stmt and _rc_line_configures_bin_dir(stmt, targets=targets, home_str=home_str):
                return True
    return False


def _posix_system_path_contains(bin_dir: Path, platform_name: str) -> bool:
    norm = _normalize_path_entry(str(bin_dir), platform_name)
    if norm in _POSIX_SYSTEM_PATH_DIRS:
        return True
    if platform_name == "darwin" and sys.platform == "darwin":
        try:
            etc_paths = Path("/etc/paths").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        for line in etc_paths.splitlines():
            entry = line.strip()
            if entry and not entry.startswith("#") and _normalize_path_entry(entry, platform_name) == norm:
                return True
    return False


def _find_posix_persistent_rc(bin_dir: Path, home: Path, shell_name: str) -> Path | None:
    bin_text = str(bin_dir).rstrip("/") or "/"
    home_str = str(home).rstrip("/") or "/"
    targets = {bin_text}
    try:
        rel = bin_dir.relative_to(home).as_posix()
    except ValueError:
        rel = ""
    if rel and rel != ".":
        targets.add(f"{home_str}/{rel}")
    for rc in _posix_startup_files(home, shell_name):
        try:
            if not rc.is_file():
                continue
            content = rc.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _rc_configures_bin_dir(
            content,
            targets=targets,
            home_str=home_str,
            is_fish_vars=(rc.name == "fish_variables"),
        ):
            return rc
    return None


def _display_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = parsed.port
        netloc = f"{host}:{port}" if port is not None else host
        return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except ValueError:
        return "<invalid URL>"


def _format_source_line(repo: str, ref: str, archive_url: str) -> str:
    if archive_url:
        return f"Updating ViperCapture from `{_display_url(archive_url)}`..."
    return f"Updating ViperCapture from `https://github.com/{repo}` ({ref})..."


def _format_update_transition(
    old_version: str,
    new_version: str,
    old_sha: str | None,
    new_sha: str | None,
) -> str:
    if old_version and new_version:
        if old_version != new_version:
            return f"Updating ViperCapture from {old_version} to {new_version}"
        old_short = _short_sha(old_sha)
        new_short = _short_sha(new_sha)
        if old_short and new_short and old_short != new_short:
            return f"Updating ViperCapture from {old_version} ({old_short}) to {new_version} ({new_short})"
        return f"Updating ViperCapture {new_version}"
    if new_version:
        return f"Updating ViperCapture to {new_version}"
    if old_version:
        return f"Updating ViperCapture from {old_version}"
    return "Updating ViperCapture"


def _format_resolved_version(
    new_version: str,
    remote_sha: str | None,
    ref: str,
    *,
    custom_archive: bool,
) -> str:
    short = "" if custom_archive else _short_sha(remote_sha)
    if new_version and short:
        return f"{new_version} ({short})"
    if new_version:
        return new_version
    if short:
        return short
    if custom_archive:
        return "custom archive"
    return ref


def _command_name(shim: Path, *, platform_name: str, shell_name: str) -> str:
    if (
        platform_name == "win32"
        and shell_name not in ("PowerShell", "Command Prompt")
        and shim.suffix.lower() == ".cmd"
    ):
        return shim.name
    return "vipercapture"


def _format_current_command(
    shim: Path,
    *,
    platform_name: str,
    shell_name: str,
    on_current_path: bool,
    stream=None,
) -> str:
    cmd_name = _command_name(shim, platform_name=platform_name, shell_name=shell_name)
    if on_current_path:
        return cmd_name
    text = str(shim)
    if shell_name == "PowerShell":
        command = f"& '{text.replace(chr(39), chr(39) * 2)}'"
    elif shell_name == "Command Prompt":
        if any(char in text for char in '%!"'):
            return f"run the installed {cmd_name} binary from your install bin directory"
        command = f'"{text}"'
    elif platform_name == "win32" and shell_name == "terminal":
        command = f"run {text}"
    else:
        if platform_name == "win32":
            text = text.replace("\\", "/")
        command = shlex.quote(text)
    target_stream = sys.stdout if stream is None else stream
    if not _supports_unicode_text(target_stream, command):
        return f"run the installed {cmd_name} binary from your install bin directory"
    return command


def _format_future_line(
    bin_dir: Path,
    *,
    cmd_name: str,
    shell_name: str,
    on_future_path: bool,
    shadowed_cmd: str | None = None,
    stream=None,
) -> str:
    if shell_name == "PowerShell":
        label = "Future PowerShell windows"
        reopen = f"open a new PowerShell window and run: {cmd_name}"
    elif shell_name == "Command Prompt":
        label = "Future Command Prompt windows"
        reopen = f"open a new Command Prompt window and run: {cmd_name}"
    elif shell_name == "terminal":
        label = "Future terminal sessions"
        reopen = f"open a new terminal and run: {cmd_name}"
    else:
        label = f"Future {shell_name} sessions"
        reopen = f"open a new terminal and run: {cmd_name}"
    if on_future_path:
        return f"{label}: {reopen}"
    if shadowed_cmd:
        return f"{label}: {shadowed_cmd}"
    target_stream = sys.stdout if stream is None else stream
    dir_text = str(bin_dir)
    if not _supports_unicode_text(target_stream, dir_text):
        return f"{label}: add the install bin directory to PATH, then run: {cmd_name}"
    return f"{label}: add {dir_text} to PATH, then run: {cmd_name}"


def _supports_unicode_text(stream, text: str) -> bool:
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def _safe_display_path(path: Path, fallback: str, stream=None) -> str:
    text = str(path)
    target_stream = sys.stdout if stream is None else stream
    if _supports_unicode_text(target_stream, text):
        return text
    return fallback


def _emit(line: str = "") -> None:
    if not _supports_unicode_text(sys.stdout, line):
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        try:
            line = line.encode(encoding, errors="replace").decode(encoding, errors="replace")
        except LookupError:
            line = line.encode("ascii", errors="replace").decode("ascii")
    print(line, flush=True)


def run_update(
    root: Path,
    *,
    fetch: Fetch = fetch_bytes,
    repo: str | None = None,
    ref: str | None = None,
    archive_url: str | None = None,
    platform_name: str | None = None,
    machine: str | None = None,
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
            machine=machine,
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
    machine: str | None,
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
    if not (root / "launch.py").is_file() or root.name != "app":
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
    shell_arg = shell
    if shell is None:
        shell = os.environ.get("SHELL", "")
    if server_running is None:
        server_running = server_is_running

    prefix = root.parent
    local_sha = read_revision(prefix)
    local_version = read_version(root)
    platform_label = detect_platform_label(platform_name, machine=machine)

    if not check_only:
        _emit()
        _emit(_format_source_line(repo, ref, archive_url))

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
            _emit(f"  {label} is already up to date.")
            return 0
        _emit(f"  An update is available for {label}.")
        return 10
    if current:
        _emit(f"==> Detected platform: {platform_label}")
        _emit(f"==> {label} is already up to date.")
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
        new_version = read_version(source)
        effective_sha = None if archive_url else remote_sha
        _emit(f"==> {_format_update_transition(local_version, new_version, local_sha, effective_sha)}")
        _emit(f"==> Detected platform: {platform_label}")
        _emit(
            f"==> Resolved version: {_format_resolved_version(new_version, remote_sha, ref, custom_archive=bool(archive_url))}"
        )
        replace_install_tree(source, root)
    new_version = read_version(root) or new_version
    python = shim_python(executable, root)
    shims = shims_for_install(
        root, platform_name=platform_name, path_env=path_env, home=home
    )
    created_path: Path | None = None
    for shim in shims:
        if write_shim(shim, python, root / "launch.py", platform_name) and created_path is None:
            created_path = shim
    # A custom archive or an API miss is not the SHA we just checked.
    # Drop any older stamp so the next plain update does not treat it as current.
    if remote_sha and not archive_url:
        write_revision(prefix, remote_sha)
    else:
        (prefix / "revision").unlink(missing_ok=True)

    primary_shim = created_path or shims[0]
    primary_bin_dir = primary_shim.parent
    shell_name = _detect_shell_name(
        platform_name,
        shell_arg,
        via_cmd_shim=(platform_name == "win32" and primary_shim.suffix.lower() == ".cmd"),
    )
    cmd_name = _command_name(primary_shim, platform_name=platform_name, shell_name=shell_name)
    on_current_path = _path_contains(path_env, primary_bin_dir, platform_name)
    cmd_cwd: Path | None = None
    if (
        platform_name == "win32"
        and sys.platform == "win32"
        and shell_name == "Command Prompt"
        and "NoDefaultCurrentDirectoryInExePath" not in os.environ
    ):
        try:
            cmd_cwd = Path.cwd()
        except OSError:
            cmd_cwd = None
    bare_cmd_works = (on_current_path or cmd_cwd is not None) and _resolves_to_install_shim(
        path_env, cmd_name, shims, platform_name, shell_name=shell_name, cwd=cmd_cwd
    )

    written_rc: Path | None = None
    configured_rc: Path | None = None
    path_status = "missing"
    on_persistent_path = False
    future_cmd_works = False
    if created_path is not None:
        if platform_name == "win32":
            path_status = ensure_windows_path(primary_bin_dir)
            on_persistent_path = path_status in ("added", "present")
            if on_persistent_path:
                persistent = _read_windows_persistent_path()
                future_cmd_works = (
                    _resolves_to_install_shim(
                        persistent, cmd_name, shims, "win32", shell_name=shell_name
                    )
                    if persistent is not None
                    else True
                )
        else:
            path_status = ensure_posix_path(primary_bin_dir, home, shell)
            written_rc = posix_rc_file(home, shell)
            configured_rc = _find_posix_persistent_rc(primary_bin_dir, home, shell_name)
            on_persistent_path = configured_rc is not None or (
                on_current_path and _posix_system_path_contains(primary_bin_dir, platform_name)
            )
            future_cmd_works = on_persistent_path and (not on_current_path or bare_cmd_works)
    else:
        if platform_name == "win32":
            persistent = _read_windows_persistent_path()
            if persistent is not None and _path_contains(persistent, primary_bin_dir, "win32"):
                on_persistent_path = True
                path_status = "present"
                future_cmd_works = _resolves_to_install_shim(
                    persistent, cmd_name, shims, "win32", shell_name=shell_name
                )
        else:
            configured_rc = _find_posix_persistent_rc(primary_bin_dir, home, shell_name)
            if configured_rc is not None or (
                on_current_path and _posix_system_path_contains(primary_bin_dir, platform_name)
            ):
                on_persistent_path = True
                path_status = "present"
                future_cmd_works = not on_current_path or bare_cmd_works

    bin_label_mid = _safe_display_path(primary_bin_dir, "the install bin directory")
    bin_label_start = _safe_display_path(primary_bin_dir, "The install bin directory")
    written_rc_label = (
        _safe_display_path(written_rc, "your shell profile") if written_rc is not None else ""
    )
    configured_rc_label = (
        _safe_display_path(configured_rc, "your shell profile") if configured_rc is not None else ""
    )
    if path_status == "added" and (
        platform_name == "win32"
        or (written_rc is not None and _shell_reads_posix_rc(shell_name, written_rc, home))
        or not on_persistent_path
    ):
        if platform_name == "win32":
            _emit(f"==> Added {bin_label_mid} to PATH.")
        elif on_persistent_path:
            _emit(f"==> Added {bin_label_mid} to PATH in {written_rc_label}.")
        elif shell_name == "terminal":
            _emit(f"==> Added {bin_label_mid} to PATH in {written_rc_label} (may not be loaded by your shell).")
        else:
            _emit(f"==> Added {bin_label_mid} to PATH in {written_rc_label} (not loaded by {shell_name}).")
    elif on_current_path and on_persistent_path:
        _emit(f"==> {bin_label_start} is already on PATH.")
    elif on_current_path:
        _emit(f"==> {bin_label_start} is on PATH for this session.")
    elif on_persistent_path:
        if platform_name == "win32" or not configured_rc_label:
            _emit(f"==> {bin_label_start} is configured on PATH.")
        else:
            _emit(f"==> {bin_label_start} is configured on PATH in {configured_rc_label}.")
    else:
        _emit(f"==> {bin_label_start} is not on PATH.")

    current_cmd = _format_current_command(
        primary_shim,
        platform_name=platform_name,
        shell_name=shell_name,
        on_current_path=bare_cmd_works,
    )
    shadowed_future_cmd: str | None = None
    if on_persistent_path and not future_cmd_works:
        shadowed_future_cmd = _format_current_command(
            primary_shim,
            platform_name=platform_name,
            shell_name=shell_name,
            on_current_path=False,
        )
    session_label = (
        "Current terminal session" if shell_name == "terminal" else f"Current {shell_name} session"
    )
    _emit(f"==> {session_label}: {current_cmd}")
    _emit(
        f"==> {_format_future_line(primary_bin_dir, cmd_name=cmd_name, shell_name=shell_name, on_future_path=future_cmd_works, shadowed_cmd=shadowed_future_cmd)}"
    )
    _emit(f"{_install_label(new_version)} installed successfully.")
    if server_running():
        _emit("==> A server is still running on http://127.0.0.1:8000.")
    _emit()
    banner_prefix = "🎉 " if _supports_unicode_text(sys.stdout, "🎉") else ""
    _emit(f"{banner_prefix}Update ran successfully! Please restart ViperCapture.")
    return 0
