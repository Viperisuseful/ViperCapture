from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from typing import Callable
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import launch  # noqa: E402
import updater  # noqa: E402


class ReadyBannerTests(unittest.TestCase):
    def test_banner_names_the_page_the_api_and_how_to_stop(self) -> None:
        banner = launch.ready_banner("http://127.0.0.1:8000/")
        self.assertIn(
            "ViperCapture is up on http://127.0.0.1:8000 and API is listening on http://127.0.0.1:8000/v1",
            banner,
        )
        self.assertIn(
            "Press Esc twice or Ctrl+C to close this (or kill the terminal)",
            banner,
        )


class TerminalLogoTests(unittest.TestCase):
    def test_logo_png_is_the_shipped_mark(self) -> None:
        png = (ROOT / "static" / "vipercapture-mark.png").read_bytes()
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_graphics_for_image_terminals_including_windows(self) -> None:
        kitty = {"TERM": "xterm-kitty"}
        self.assertEqual(
            launch.choose_graphics_protocol(
                platform_name="win32", is_tty=True, environ={"KITTY_WINDOW_ID": "1", **kitty}
            ),
            "kitty",
        )
        self.assertFalse(
            launch.terminal_graphics_supported(platform_name="linux", is_tty=False, environ=kitty)
        )
        self.assertFalse(
            launch.terminal_graphics_supported(
                platform_name="linux", is_tty=True, environ={"TERM": "xterm-256color"}
            )
        )
        self.assertFalse(
            launch.terminal_graphics_supported(
                platform_name="win32", is_tty=True, environ={"TERM": "xterm-256color"}
            )
        )
        self.assertFalse(
            launch.terminal_graphics_supported(
                platform_name="linux", is_tty=True, environ={**kitty, "TMUX": "/tmp/tmux-1000"}
            )
        )
        self.assertEqual(
            launch.choose_graphics_protocol(platform_name="linux", is_tty=True, environ=kitty),
            "kitty",
        )
        self.assertEqual(
            launch.choose_graphics_protocol(
                platform_name="darwin",
                is_tty=True,
                environ={"KITTY_WINDOW_ID": "1", "TERM": "xterm-256color"},
            ),
            "kitty",
        )
        self.assertEqual(
            launch.choose_graphics_protocol(
                platform_name="linux", is_tty=True, environ={"TERM": "xterm-ghostty"}
            ),
            "kitty",
        )
        self.assertEqual(
            launch.choose_graphics_protocol(
                platform_name="darwin",
                is_tty=True,
                environ={"TERM_PROGRAM": "ghostty", "TERM": "xterm-256color"},
            ),
            "kitty",
        )
        self.assertEqual(
            launch.choose_graphics_protocol(
                platform_name="linux",
                is_tty=True,
                environ={"GHOSTTY_RESOURCES_DIR": "/usr/share/ghostty", "TERM": "xterm-256color"},
            ),
            "kitty",
        )
        wezterm = {"TERM_PROGRAM": "WezTerm", "TERM": "xterm-256color", "WEZTERM_PANE": "1"}
        self.assertEqual(
            launch.choose_graphics_protocol(
                platform_name="win32", is_tty=True, environ=wezterm, kitty_graphics=False
            ),
            "iterm2",
        )
        self.assertEqual(
            launch.choose_graphics_protocol(
                platform_name="linux", is_tty=True, environ=wezterm, kitty_graphics=True
            ),
            "kitty",
        )
        self.assertIsNone(
            launch.choose_graphics_protocol(
                platform_name="linux",
                is_tty=True,
                environ={**wezterm, "TMUX": "1"},
                kitty_graphics=True,
            )
        )
        self.assertEqual(
            launch.choose_graphics_protocol(
                platform_name="darwin",
                is_tty=True,
                environ={"TERM_PROGRAM": "iTerm.app", "ITERM_SESSION_ID": "w0t0p0", "TERM": "xterm-256color"},
            ),
            "iterm2",
        )
        self.assertEqual(
            launch.choose_graphics_protocol(
                platform_name="linux", is_tty=True, environ={"TERM": "foot"}
            ),
            "sixel",
        )
        self.assertEqual(
            launch.choose_graphics_protocol(
                platform_name="win32",
                is_tty=True,
                environ={"WT_SESSION": "abc", "TERM": "xterm-256color"},
            ),
            "sixel",
        )

    def test_wezterm_kitty_graphics_follows_the_config_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wezterm.lua"
            path.write_text(
                "config.enable_kitty_graphics = false\n"
                "-- enable_kitty_graphics = true\n"
                "config.enable_kitty_graphics = true\n",
                encoding="utf-8",
            )
            env = {"WEZTERM_CONFIG_FILE": str(path)}
            self.assertTrue(launch.wezterm_config_enables_kitty_graphics(env))
            path.write_text(
                "--[[\nenable_kitty_graphics = true\n]]\n"
                "config.enable_kitty_graphics = false\n",
                encoding="utf-8",
            )
            self.assertFalse(launch.wezterm_config_enables_kitty_graphics(env))
            missing = {"WEZTERM_CONFIG_FILE": str(Path(directory) / "missing.lua")}
            self.assertFalse(launch.wezterm_config_enables_kitty_graphics(missing))

    def test_iterm2_and_sixel_sequences_carry_the_logo(self) -> None:
        png = (ROOT / "static" / "vipercapture-mark.png").read_bytes()
        width, height, pixels = launch.png_rgba(png)
        self.assertEqual((width, height), (256, 256))
        self.assertEqual(len(pixels), 256 * 256 * 4)
        center = (128 * 256 + 128) * 4
        self.assertGreater(pixels[center + 2], pixels[center])
        self.assertGreater(pixels[center + 3], 200)

        iterm = launch.iterm2_image_sequence(png, width=12, height=6)
        self.assertTrue(iterm.startswith(b"\x1b]1337;File=inline=1;preserveAspectRatio=1;"))
        self.assertIn(b"width=12", iterm)
        self.assertIn(b"height=6", iterm)
        self.assertTrue(iterm.endswith(b"\a"))
        payload = iterm.split(b":", 1)[1][:-1]
        self.assertEqual(base64.standard_b64decode(payload), png)

        red = bytes([255, 0, 0, 255]) * 6
        sixel = launch.encode_sixel(1, 6, red)
        self.assertTrue(sixel.startswith(b"\x1bPq"))
        self.assertIn(b'"1;1;1;6', sixel)
        self.assertIn(b"#1;2;100;0;0", sixel)
        self.assertIn(b"~", sixel)
        self.assertTrue(sixel.endswith(b"\x1b\\"))
        clear = launch.encode_sixel(1, 6, bytes([0, 0, 0, 0]) * 6)
        self.assertNotIn(b"#1", clear)

        placed = launch.place_logo(png, "sixel", cols=8, rows=4, size=launch.WindowSize(80, 24, 800, 480))
        self.assertTrue(placed.startswith(b"\x1bPq"))
        self.assertIn(b'"1;1;80;80', placed)

    def test_status_frame_uses_the_terminal_protocol(self) -> None:
        png = (ROOT / "static" / "vipercapture-mark.png").read_bytes()
        size = launch.WindowSize(100, 30, 1000, 600)
        iterm = launch.render_status_frame(size, "http://127.0.0.1:8000/", png, protocol="iterm2")
        self.assertIn(b"\x1b]1337;File=", iterm)
        self.assertNotIn(b"a=T,f=100", iterm)
        sixel = launch.render_status_frame(size, "http://127.0.0.1:8000/", png, protocol="sixel")
        self.assertIn(b"\x1bPq", sixel)
        self.assertIn(b"Press Esc twice or Ctrl+C to close this", sixel)

    def test_sequence_transmits_png_quietly_in_4096_byte_chunks(self) -> None:
        png = (ROOT / "static" / "vipercapture-mark.png").read_bytes()
        sequence = launch.kitty_graphics_sequence(png, rows=6)
        self.assertTrue(sequence.startswith(b"\x1b_Ga=T,f=100,t=d,q=2,r=6,m=1;"))
        self.assertIn(b"\x1b_Gm=0;", sequence)
        self.assertTrue(sequence.endswith(b"\x1b\\\r"))
        payloads = []
        for part in sequence.split(b"\x1b\\")[:-1]:
            control, payload = part.split(b"\x1b_G", 1)[1].split(b";", 1)
            self.assertLessEqual(len(payload), 4096)
            self.assertEqual(len(payload) % 4, 0)
            payloads.append(payload)
            self.assertIn(b"m=", control)
        self.assertEqual(base64.standard_b64decode(b"".join(payloads)), png)

    def test_show_writes_nothing_for_a_plain_terminal(self) -> None:
        environ = {"TERM": "xterm-256color", "PATH": os.environ.get("PATH", "")}
        with mock.patch.dict(os.environ, environ, clear=True):
            with mock.patch.object(sys.stdout, "isatty", return_value=True):
                with mock.patch.object(sys.stdout.buffer, "write") as write:
                    launch.show_terminal_logo()
        write.assert_not_called()


class StatusScreenTests(unittest.TestCase):
    def test_logo_grows_with_the_window_and_stays_square(self) -> None:
        small = launch.WindowSize(80, 24, 800, 480)
        large = launch.WindowSize(160, 50, 1600, 1000)
        small_cols, small_rows = launch.logo_cells(small)
        large_cols, large_rows = launch.logo_cells(large)
        self.assertGreater(large_cols, small_cols)
        self.assertGreater(large_rows, small_rows)
        cell_w = large.xpixels / large.cols
        cell_h = large.ypixels / large.rows
        self.assertLess(abs(large_cols * cell_w - large_rows * cell_h), cell_w + cell_h)

    def test_logo_is_omitted_on_a_tiny_terminal(self) -> None:
        self.assertEqual(launch.logo_cells(launch.WindowSize(20, 8, 200, 160)), (0, 0))

    def test_status_text_is_centered_under_the_logo(self) -> None:
        size = launch.WindowSize(80, 24, 800, 480)
        layout = launch.status_layout(size, "http://127.0.0.1:8000/")
        self.assertGreater(layout.logo_cols, 0)
        self.assertLess(layout.logo_col, size.cols // 2)
        self.assertGreater(layout.text_row, layout.logo_row)
        text = "ViperCapture is up on http://127.0.0.1:8000"
        first = layout.lines[0]
        self.assertTrue(first.endswith(text))
        self.assertEqual(len(first) - len(text), (size.cols - len(text)) // 2)

    def test_frame_places_a_scaled_logo_and_the_stop_hint(self) -> None:
        png = (ROOT / "static" / "vipercapture-mark.png").read_bytes()
        frame = launch.render_status_frame(
            launch.WindowSize(100, 30, 1000, 600),
            "http://127.0.0.1:8000/",
            png,
        )
        self.assertIn(b"\x1b[2J", frame)
        self.assertIn(b"a=T,f=100,t=d,q=2,i=1,c=", frame)
        self.assertIn(b"Press Esc twice or Ctrl+C to close this", frame)
        self.assertNotIn(b"\r", frame.split(b"\x1b[2J", 1)[1][:20])

    def test_request_window_uses_the_current_terminal(self) -> None:
        log = Path("/tmp/vipercapture-requests.log")
        ghostty = launch.request_window_argv(
            "ghostty", log, 42, follow_pid=True, platform_name="linux"
        )
        self.assertEqual(ghostty[:3], ["ghostty", "+new-window", "--title=ViperCapture requests"])
        self.assertEqual(ghostty[3], "-e")
        self.assertIn("--pid", ghostty)
        self.assertIn("42", ghostty)
        self.assertIn(str(log), ghostty)
        mac_ghostty = launch.request_window_argv(
            "ghostty", log, 42, follow_pid=False, platform_name="darwin"
        )
        self.assertEqual(mac_ghostty[:4], ["open", "-na", "Ghostty.app", "--args"])
        self.assertNotIn("--pid", mac_ghostty)
        self.assertIn(str(log), mac_ghostty)
        self.assertIn("42", mac_ghostty)
        kitty_tab = launch.request_window_argv("kitty-tab", log, 7, follow_pid=True)
        self.assertEqual(kitty_tab[:4], ["kitten", "@", "launch", "--type=tab"])
        self.assertIn("--pid", kitty_tab)
        bsd_kitty = launch.request_window_argv("kitty-tab", log, 7, follow_pid=False)
        self.assertEqual(bsd_kitty[0], "kitten")
        self.assertIn("sh", bsd_kitty)
        self.assertNotIn("--pid", bsd_kitty)
        self.assertIsNone(launch.terminal_kind({"TERM": "xterm-256color"}))
        self.assertEqual(
            launch.terminal_kind({"TERM": "xterm-ghostty", "TERM_PROGRAM": "ghostty"}),
            "ghostty",
        )
        self.assertEqual(launch.terminal_kind({"KITTY_WINDOW_ID": "1", "TERM": "xterm-kitty"}), "kitty")

    def test_redirected_stdout_uses_the_banner(self) -> None:
        class Stdio:
            def __init__(self, tty: bool) -> None:
                self.buffer = io.BytesIO()
                self._text = io.StringIO()
                self._tty = tty

            def isatty(self) -> bool:
                return self._tty

            def write(self, data: str) -> int:
                return self._text.write(data)

            def flush(self) -> None:
                self._text.flush()

            def fileno(self) -> int:
                raise OSError("no terminal")

        log_path = Path("/tmp/vipercapture-requests.log")
        server = mock.Mock()

        def present(stdin_tty: bool, stdout_tty: bool) -> tuple[Stdio, mock.Mock]:
            stdout = Stdio(stdout_tty)
            with (
                mock.patch.object(sys, "stdin", Stdio(stdin_tty)),
                mock.patch.object(sys, "stdout", stdout),
                mock.patch.object(sys, "platform", "linux"),
                mock.patch.object(launch, "open_request_window", return_value=False),
                mock.patch.object(launch, "wait_for_shutdown", return_value=False) as wait,
            ):
                launch.present_running_server(server, log_path)
            return stdout, wait

        redirected, banner_wait = present(True, False)
        banner_wait.assert_called_once_with(server)
        self.assertIn("ViperCapture is up on http://127.0.0.1:8000", redirected._text.getvalue())
        self.assertIn(f"Requests: {log_path}", redirected._text.getvalue())
        self.assertNotIn(b"\x1b[?1049h", redirected.buffer.getvalue())

        interactive, screen_wait = present(True, True)
        screen_wait.assert_called_once()
        self.assertEqual(screen_wait.call_args.args, (server,))
        self.assertFalse(screen_wait.call_args.kwargs["announce"])
        self.assertIn(b"\x1b[?1049h", interactive.buffer.getvalue())
        self.assertIn(b"\x1b[?1049l", interactive.buffer.getvalue())
        self.assertNotIn("ViperCapture is up on", interactive._text.getvalue())


class LaunchArgsTests(unittest.TestCase):
    def test_one_window_flag(self) -> None:
        self.assertTrue(launch.parse_launch_args(["--one-window"]))
        self.assertFalse(launch.parse_launch_args([]))
        self.assertTrue(launch.parse_launch_args(["--one-window", "--one-window"]))

    def test_help_exits_cleanly(self) -> None:
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            with self.assertRaises(SystemExit) as caught:
                launch.parse_launch_args(["--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("--one-window", stdout.getvalue())
        self.assertIn("vipercapture update", stdout.getvalue())
        self.assertIn("Windows always uses one window.", stdout.getvalue())

    def test_unknown_argument_exits(self) -> None:
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            with self.assertRaises(SystemExit) as caught:
                launch.parse_launch_args(["--two-windows"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("Unknown argument: --two-windows", stderr.getvalue())

    def test_update_is_a_command(self) -> None:
        command = launch.parse_cli(["update"])
        self.assertTrue(command.update)
        self.assertFalse(command.check)
        self.assertFalse(command.one_window)
        checked = launch.parse_cli(["update", "--check"])
        self.assertTrue(checked.update)
        self.assertTrue(checked.check)

    def test_update_rejects_launch_flags(self) -> None:
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            with self.assertRaises(SystemExit) as caught:
                launch.parse_cli(["update", "--one-window"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("Unknown argument: --one-window", stderr.getvalue())

    def test_windows_stays_in_one_window_without_the_flag(self) -> None:
        self.assertTrue(launch.use_one_window(False, "win32"))
        self.assertFalse(launch.use_one_window(False, "linux"))
        self.assertFalse(launch.use_one_window(False, "darwin"))
        self.assertTrue(launch.use_one_window(True, "linux"))


class StopKeyBufferTests(unittest.TestCase):
    def test_two_separate_escapes_stop(self) -> None:
        keys = launch.StopKeyBuffer()
        self.assertFalse(keys.feed(b"\x1b"))
        self.assertFalse(keys.flush())
        self.assertFalse(keys.feed(b"\x1b"))
        self.assertTrue(keys.flush())

    def test_two_escapes_in_one_read_stop_after_flush(self) -> None:
        keys = launch.StopKeyBuffer()
        self.assertFalse(keys.feed(b"\x1b\x1b"))
        self.assertTrue(keys.flush())

    def test_arrow_keys_do_not_count(self) -> None:
        keys = launch.StopKeyBuffer()
        self.assertFalse(keys.feed(b"\x1b[A\x1b[B"))
        self.assertFalse(keys.flush())
        self.assertEqual(keys.presses, 0)

    def test_one_escape_does_not_stop(self) -> None:
        keys = launch.StopKeyBuffer()
        self.assertFalse(keys.feed(b"\x1b"))
        self.assertFalse(keys.flush())
        self.assertEqual(keys.presses, 1)


class ProjectVenvTests(unittest.TestCase):
    def test_symlink_to_system_python_is_not_the_venv(self) -> None:
        # Arch and Omarchy point .venv/bin/python at /usr/bin/python3.
        self.assertFalse(launch.in_project_venv("/usr", "/home/app/.venv"))
        self.assertFalse(
            launch.in_project_venv("/usr/bin", "/home/app/.venv")
        )

    def test_project_prefix_is_the_venv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            venv = Path(tmp) / ".venv"
            venv.mkdir()
            self.assertTrue(launch.in_project_venv(venv, venv))
            self.assertFalse(launch.in_project_venv(Path(tmp), venv))


class FindUvTests(unittest.TestCase):
    def test_find_uv_returns_which_result(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VIPERCAPTURE_USE_UV", None)
            with mock.patch("launch.shutil.which", return_value="/opt/uv") as which:
                self.assertEqual(launch.find_uv(), "/opt/uv")
                which.assert_called_once_with("uv")

    def test_find_uv_can_be_disabled(self) -> None:
        with mock.patch.dict(os.environ, {"VIPERCAPTURE_USE_UV": "0"}):
            with mock.patch("launch.shutil.which", return_value="/opt/uv") as which:
                self.assertIsNone(launch.find_uv())
                which.assert_not_called()


class InstallerCommandTests(unittest.TestCase):
    def test_venv_prefers_uv(self) -> None:
        venv_dir = Path("/tmp/.venv")
        command = launch.venv_command("/usr/bin/python3", venv_dir, "/opt/uv")
        self.assertEqual(
            command,
            ["/opt/uv", "venv", "--python", "/usr/bin/python3", str(venv_dir)],
        )

    def test_playwright_with_deps_only_when_apt_exists(self) -> None:
        debian = launch.playwright_install_command(
            "/venv/bin/python", platform_name="linux", has_apt=True
        )
        arch = launch.playwright_install_command(
            "/venv/bin/python", platform_name="linux", has_apt=False
        )
        macos = launch.playwright_install_command(
            "/venv/bin/python", platform_name="darwin", has_apt=False
        )
        self.assertIn("--with-deps", debian)
        self.assertNotIn("--with-deps", arch)
        self.assertNotIn("--with-deps", macos)
        self.assertEqual(debian[:5], ["/venv/bin/python", "-m", "playwright", "install", "--no-shell"])

    def test_venv_falls_back_to_stdlib(self) -> None:
        venv_dir = Path("/tmp/.venv")
        command = launch.venv_command("/usr/bin/python3", venv_dir, None)
        self.assertEqual(
            command,
            ["/usr/bin/python3", "-m", "venv", str(venv_dir)],
        )

    def test_deps_prefer_uv_pip(self) -> None:
        requirements = Path("/app/requirements.txt")
        commands = launch.deps_commands("/venv/bin/python", requirements, "/opt/uv")
        self.assertEqual(
            commands,
            [(
                [
                    "/opt/uv",
                    "pip",
                    "install",
                    "--python",
                    "/venv/bin/python",
                    "-r",
                    str(requirements),
                ],
                "uv pip install",
            )],
        )

    def test_deps_fall_back_to_pip(self) -> None:
        requirements = Path("/app/requirements.txt")
        commands = launch.deps_commands("/venv/bin/python", requirements, None)
        self.assertEqual(
            [label for _command, label in commands],
            ["pip upgrade", "pip install"],
        )
        self.assertEqual(
            commands[1][0],
            [
                "/venv/bin/python",
                "-m",
                "pip",
                "install",
                "-r",
                str(requirements),
            ],
        )

    def test_deps_force_cryptography_sdist_on_intel_macos(self) -> None:
        requirements = Path("/app/requirements.txt")
        uv_commands = launch.deps_commands(
            "/venv/bin/python", requirements, "/opt/uv", intel_macos=True
        )
        pip_commands = launch.deps_commands(
            "/venv/bin/python", requirements, None, intel_macos=True
        )
        self.assertEqual(
            uv_commands[0][0][-2:],
            ["--no-binary", "cryptography"],
        )
        self.assertEqual(
            pip_commands[1][0][-2:],
            ["--no-binary", "cryptography"],
        )


class IntelMacosCryptographyTests(unittest.TestCase):
    def test_is_intel_macos_only_darwin_x86_64(self) -> None:
        self.assertTrue(launch.is_intel_macos("darwin", "x86_64"))
        self.assertFalse(launch.is_intel_macos("darwin", "arm64"))
        self.assertFalse(launch.is_intel_macos("linux", "x86_64"))
        self.assertFalse(launch.is_intel_macos("win32", "AMD64"))

    def test_parse_rustc_version(self) -> None:
        self.assertEqual(
            launch.parse_rustc_version("rustc 1.85.0 (hash 2026-01-01)"),
            (1, 85, 0),
        )
        self.assertLess(launch.parse_rustc_version("rustc 1.82.0"), launch.MIN_RUSTC)
        self.assertIsNone(launch.parse_rustc_version("not rustc"))

    def test_prepare_skips_non_intel_macos(self) -> None:
        missing = launch.prepare_intel_macos_cryptography_build(
            sys_platform="linux",
            machine="x86_64",
        )
        self.assertEqual(missing, [])

    def _openssl_prefix(self, root: Path) -> str:
        include = root / "include" / "openssl"
        include.mkdir(parents=True)
        (include / "ssl.h").write_text("/* test */\n", encoding="utf-8")
        (include / "opensslv.h").write_text(
            "# define OPENSSL_VERSION_MAJOR  3\n"
            "# define OPENSSL_VERSION_MINOR  5\n",
            encoding="utf-8",
        )
        lib = root / "lib" / "pkgconfig"
        lib.mkdir(parents=True)
        (root / "lib" / "libcrypto.dylib").write_bytes(b"")
        (root / "lib" / "libssl.dylib").write_bytes(b"")
        return str(root)

    def _ready_which(self, **extra: str) -> Callable[[str], str | None]:
        mapping = {
            "cc": "/usr/bin/cc",
            "rustc": "/usr/local/bin/rustc",
            "cargo": "/usr/local/bin/cargo",
            "brew": "/usr/local/bin/brew",
            **extra,
        }

        def which(name: str) -> str | None:
            return mapping.get(name)

        return which

    def _ready_run(self, openssl_prefix: str | None = None):
        def run(cmd, **_kwargs):
            if cmd[:2] == ["/usr/bin/cc", "-v"]:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="", stderr="Apple clang version 17.0.0\n"
                )
            if cmd[:2] == ["/usr/local/bin/rustc", "--version"]:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="rustc 1.85.0 (hash)\n", stderr=""
                )
            if cmd[:2] == ["/usr/local/bin/cargo", "--version"]:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="cargo 1.85.0 (hash)\n", stderr=""
                )
            if (
                openssl_prefix
                and cmd[:3] == ["/usr/local/bin/brew", "--prefix", "openssl@3"]
            ):
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=openssl_prefix + "\n", stderr=""
                )
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

        return run

    def test_preflight_reports_missing_source_build_tools(self) -> None:
        toolchain = launch.probe_intel_macos_cryptography_toolchain(
            which=lambda _name: None,
            run=lambda *_args, **_kwargs: subprocess.CompletedProcess(
                [], 1, stdout="", stderr=""
            ),
            environ={},
        )
        missing = launch.intel_macos_cryptography_missing(toolchain)
        self.assertIn("Xcode command line tools (clang)", missing)
        self.assertIn("Rust 1.83.0+ (rustc and cargo)", missing)
        self.assertIn("Homebrew/MacPorts OpenSSL 3 (not Apple LibreSSL)", missing)
        message = launch.format_intel_macos_cryptography_error(missing)
        self.assertIn(launch.INTEL_MACOS_CRYPTOGRAPHY_DOCS, message)
        self.assertIn(launch.INTEL_MACOS_CRYPTOGRAPHY_CHANGELOG, message)
        self.assertIn("brew install openssl@3 rust", message)
        self.assertIn("xcode-select --install", message)
        self.assertIn("Do not pin cryptography <=48", message)

    def test_prepare_provisions_openssl_env_when_toolchain_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = self._openssl_prefix(Path(tmp) / "openssl@3")
            environ: dict[str, str] = {}
            missing = launch.prepare_intel_macos_cryptography_build(
                sys_platform="darwin",
                machine="x86_64",
                which=self._ready_which(),
                run=self._ready_run(prefix),
                environ=environ,
            )
            self.assertEqual(missing, [])
            self.assertEqual(environ["OPENSSL_DIR"], prefix)
            self.assertTrue(
                environ["PKG_CONFIG_PATH"].startswith(
                    str(Path(prefix) / "lib" / "pkgconfig")
                )
            )

    def test_prepare_rejects_old_rustc(self) -> None:
        def run(cmd, **_kwargs):
            if cmd[:2] == ["/usr/bin/cc", "-v"]:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="", stderr="Apple clang version 17.0.0\n"
                )
            if cmd[:2] == ["/usr/bin/rustc", "--version"]:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="rustc 1.70.0\n", stderr=""
                )
            if cmd[:2] == ["/usr/bin/cargo", "--version"]:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="cargo 1.70.0\n", stderr=""
                )
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

        missing = launch.prepare_intel_macos_cryptography_build(
            sys_platform="darwin",
            machine="x86_64",
            which=lambda name: {
                "cc": "/usr/bin/cc",
                "rustc": "/usr/bin/rustc",
                "cargo": "/usr/bin/cargo",
            }.get(name),
            run=run,
            environ={},
        )
        self.assertIn("Rust 1.83.0+ (rustc and cargo)", missing)

    def test_rejects_compiler_stub_that_cannot_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = self._openssl_prefix(Path(tmp) / "openssl@3")

            def run(cmd, **_kwargs):
                if cmd[:2] == ["/usr/bin/cc", "-v"]:
                    return subprocess.CompletedProcess(
                        cmd,
                        1,
                        stdout="",
                        stderr="xcode-select: note: no developer tools were found\n",
                    )
                return self._ready_run(prefix)(cmd)

            missing = launch.prepare_intel_macos_cryptography_build(
                sys_platform="darwin",
                machine="x86_64",
                which=self._ready_which(),
                run=run,
                environ={},
            )
            self.assertIn("Xcode command line tools (clang)", missing)

    def test_rejects_rustc_without_cargo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = self._openssl_prefix(Path(tmp) / "openssl@3")
            mapping = {
                "cc": "/usr/bin/cc",
                "rustc": "/usr/local/bin/rustc",
                "brew": "/usr/local/bin/brew",
            }

            missing = launch.prepare_intel_macos_cryptography_build(
                sys_platform="darwin",
                machine="x86_64",
                which=lambda name: mapping.get(name),
                run=self._ready_run(prefix),
                environ={},
            )
            self.assertIn("Rust 1.83.0+ (rustc and cargo)", missing)

    def test_skips_header_only_openssl_and_uses_brew(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stale = Path(tmp) / "stale"
            (stale / "include" / "openssl").mkdir(parents=True)
            (stale / "include" / "openssl" / "ssl.h").write_text(
                "/* headers only */\n", encoding="utf-8"
            )
            brew_prefix = self._openssl_prefix(Path(tmp) / "openssl@3")
            environ = {"OPENSSL_DIR": str(stale)}
            missing = launch.prepare_intel_macos_cryptography_build(
                sys_platform="darwin",
                machine="x86_64",
                which=self._ready_which(),
                run=self._ready_run(brew_prefix),
                environ=environ,
            )
            self.assertEqual(missing, [])
            self.assertEqual(environ["OPENSSL_DIR"], brew_prefix)

    def test_rejects_libressl_prefix(self) -> None:
        self.assertFalse(
            launch.openssl_headers_are_usable(
                "# define LIBRESSL_VERSION_NUMBER 0x40000000L\n"
                "# define OPENSSL_VERSION_NUMBER  0x20000000L\n"
            )
        )
        self.assertTrue(
            launch.openssl_headers_are_usable("# define OPENSSL_VERSION_MAJOR  3\n")
        )
        self.assertFalse(
            launch.openssl_headers_are_usable(
                "# define OPENSSL_VERSION_NUMBER  0x101010cfL\n"
            )
        )

    def test_requirements_keep_patched_floors(self) -> None:
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn(
            'cryptography>=49.0.0; sys_platform == "darwin" and platform_machine == "x86_64"',
            text,
        )
        self.assertIn(
            'cryptography>=50.0.0; sys_platform != "darwin" or platform_machine != "x86_64"',
            text,
        )
        self.assertNotIn("<47.0.0", text)
        self.assertNotIn("<=48", text)


class InstallScriptTests(unittest.TestCase):
    def test_install_script_creates_a_user_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            source = root / "source"
            home.mkdir()
            source.mkdir()
            (source / "launch.py").write_text("print('vipercapture')\n", encoding="utf-8")
            (source / "requirements.txt").write_text("\n", encoding="utf-8")
            nested = source / "frontend" / "node_modules"
            nested.mkdir(parents=True)
            (nested / "junk.txt").write_text("nope\n", encoding="utf-8")
            (source / "keep.txt").write_text("yes\n", encoding="utf-8")

            def run_install() -> subprocess.CompletedProcess[str]:
                env = os.environ.copy()
                env["HOME"] = str(home)
                env["VIPERCAPTURE_SOURCE"] = str(source)
                env["SHELL"] = "/bin/bash"
                env.pop("VIPERCAPTURE_HOME", None)
                env.pop("VIPERCAPTURE_BIN_DIR", None)
                env.pop("VIPERCAPTURE_PYTHON", None)
                # ensure_python exports this after installing uv on Python < 3.11.
                # The shim directory is on the current PATH, and the shell rc is still missing.
                bin_dir = str(home / ".local" / "bin")
                env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
                return subprocess.run(
                    ["bash", str(ROOT / "scripts" / "install.sh")],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )

            first = run_install()
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            app = home / ".vipercapture" / "app"
            shim = home / ".local" / "bin" / "vipercapture"
            self.assertTrue(shim.is_file())
            self.assertTrue(os.access(shim, os.X_OK))
            shim_text = shim.read_text(encoding="utf-8")
            self.assertIn(str(app / "launch.py"), shim_text)
            self.assertEqual((app / "keep.txt").read_text(encoding="utf-8"), "yes\n")
            self.assertFalse((app / "frontend" / "node_modules").exists())
            bashrc = home / ".bashrc"
            bashrc_text = bashrc.read_text(encoding="utf-8")
            self.assertEqual(bashrc_text.count("# ViperCapture"), 1)
            self.assertIn(f'export PATH="{shim.parent}:$PATH"', bashrc_text)

            venv = app / ".venv"
            venv.mkdir()
            (venv / "marker").write_text("keep\n", encoding="utf-8")
            second = run_install()
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertEqual((venv / "marker").read_text(encoding="utf-8"), "keep\n")
            self.assertEqual(bashrc.read_text(encoding="utf-8").count("# ViperCapture"), 1)

            ran = subprocess.run(
                [str(shim)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            self.assertIn("vipercapture", ran.stdout)

            fresh = os.environ.copy()
            fresh["HOME"] = str(home)
            fresh["PATH"] = os.pathsep.join(
                part
                for part in os.environ.get("PATH", "").split(os.pathsep)
                if part and part != str(shim.parent)
            )
            sourced = subprocess.run(
                [
                    "bash",
                    "--noprofile",
                    "--norc",
                    "-c",
                    'source "$1" && command -v vipercapture && vipercapture',
                    "bash",
                    str(bashrc),
                ],
                env=fresh,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(sourced.returncode, 0, sourced.stdout + sourced.stderr)
            self.assertIn(str(shim), sourced.stdout)
            self.assertIn("vipercapture", sourced.stdout)


OLD_SHA = "a" * 40
NEW_SHA = "b" * 40


def _github_api(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme == "https" and parsed.hostname == "api.github.com"


def _archive(source: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.add(source, arcname="ViperCapture-master")
    return buffer.getvalue()


class UpdateTests(unittest.TestCase):
    def _install(self, home: Path, version: str = "1.0.4", revision: str | None = None) -> Path:
        app = home / ".vipercapture" / "app"
        app.mkdir(parents=True)
        (app / "launch.py").write_text("print('old')\n", encoding="utf-8")
        (app / "VERSION").write_text(version + "\n", encoding="utf-8")
        (app / "old.txt").write_text("gone\n", encoding="utf-8")
        venv = app / ".venv"
        venv.mkdir()
        (venv / "marker").write_text("keep\n", encoding="utf-8")
        (home / ".vipercapture" / "async-jobs.sqlite3").write_text("jobs\n", encoding="utf-8")
        if revision:
            (home / ".vipercapture" / "revision").write_text(revision + "\n", encoding="utf-8")
        return app

    def _fetch(self, archive: bytes, sha: str = NEW_SHA) -> Callable[[str], bytes]:
        def fetch(url: str) -> bytes:
            if _github_api(url):
                return json.dumps({"sha": sha}).encode()
            return archive
        return fetch

    def test_check_reports_an_available_update_without_replacing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            app = self._install(home, revision=OLD_SHA)
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                code = updater.run_update(
                    app,
                    fetch=self._fetch(b"unused"),
                    home=home,
                    path_env="",
                    shell="/bin/bash",
                    server_running=lambda: False,
                    executable="/usr/bin/python3",
                    check_only=True,
                )
            self.assertEqual(code, 10)
            self.assertIn("An update is available for ViperCapture 1.0.4", stdout.getvalue())
            self.assertEqual((app / "launch.py").read_text(encoding="utf-8"), "print('old')\n")
            self.assertFalse((home / ".local").exists())

    def test_already_current_skips_the_download(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            app = self._install(home, revision=NEW_SHA)
            calls: list[str] = []

            def fetch(url: str) -> bytes:
                calls.append(url)
                if not _github_api(url):
                    raise AssertionError(url)
                return json.dumps({"sha": NEW_SHA}).encode()

            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                code = updater.run_update(
                    app,
                    fetch=fetch,
                    home=home,
                    path_env="",
                    shell="/bin/bash",
                    server_running=lambda: False,
                    executable="/usr/bin/python3",
                )
            self.assertEqual(code, 0, stdout.getvalue())
            self.assertEqual(len(calls), 1)
            self.assertIn("ViperCapture 1.0.4 is already up to date.", stdout.getvalue())
            self.assertEqual((app / "old.txt").read_text(encoding="utf-8"), "gone\n")

    def test_update_replaces_the_app_and_keeps_venv_and_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            app = self._install(home, revision=OLD_SHA)
            bin_dir = home / "bin"
            bin_dir.mkdir()
            shim = bin_dir / "vipercapture"
            shim.write_text(
                "#!/bin/sh\n"
                f"exec '/usr/bin/python3' '{app / 'launch.py'}' \"$@\"\n",
                encoding="utf-8",
            )
            source = home / "incoming"
            source.mkdir()
            (source / "launch.py").write_text("print('new')\n", encoding="utf-8")
            (source / "VERSION").write_text("1.0.5\n", encoding="utf-8")
            (source / "keep.txt").write_text("yes\n", encoding="utf-8")
            nested = source / "frontend" / "node_modules"
            nested.mkdir(parents=True)
            (nested / "junk.txt").write_text("nope\n", encoding="utf-8")
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                code = updater.run_update(
                    app,
                    fetch=self._fetch(_archive(source)),
                    home=home,
                    path_env=str(bin_dir),
                    shell="/bin/bash",
                    server_running=lambda: True,
                    executable="/usr/bin/python3",
                )
            self.assertEqual(code, 0, stdout.getvalue())
            self.assertEqual((app / "launch.py").read_text(encoding="utf-8"), "print('new')\n")
            self.assertEqual((app / "keep.txt").read_text(encoding="utf-8"), "yes\n")
            self.assertFalse((app / "old.txt").exists())
            self.assertFalse((app / "frontend").exists())
            self.assertEqual((app / ".venv" / "marker").read_text(encoding="utf-8"), "keep\n")
            jobs = home / ".vipercapture" / "async-jobs.sqlite3"
            self.assertEqual(jobs.read_text(encoding="utf-8"), "jobs\n")
            self.assertEqual((home / ".vipercapture" / "revision").read_text(encoding="utf-8"), NEW_SHA + "\n")
            self.assertEqual(os.stat(home / ".vipercapture" / "revision").st_mode & 0o777, 0o600)
            shim_text = shim.read_text(encoding="utf-8")
            self.assertIn("#!/bin/sh", shim_text)
            self.assertIn(str(app / "launch.py"), shim_text)
            self.assertIn("/usr/bin/python3", shim_text)
            self.assertIn("Updated ViperCapture from 1.0.4 to 1.0.5.", stdout.getvalue())
            self.assertIn("still running on http://127.0.0.1:8000", stdout.getvalue())
            self.assertFalse((home / ".vipercapture" / "app.backup").exists())
            self.assertFalse((home / ".vipercapture" / "app.updating").exists())

    def test_update_keeps_env_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            app = self._install(home, revision=OLD_SHA)
            secret = "VIPERCAPTURE_SIGNING_ADMIN_TOKEN=local-secret\n"
            local = app / ".env.local"
            local.write_text(secret, encoding="utf-8")
            os.chmod(local, 0o600)
            source = home / "incoming"
            source.mkdir()
            (source / "launch.py").write_text("print('new')\n", encoding="utf-8")
            (source / "VERSION").write_text("1.0.5\n", encoding="utf-8")
            (source / ".env.local").write_text("VIPERCAPTURE_SIGNING_ADMIN_TOKEN=from-archive\n", encoding="utf-8")
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                code = updater.run_update(
                    app,
                    fetch=self._fetch(_archive(source)),
                    home=home,
                    path_env="",
                    shell="/bin/bash",
                    server_running=lambda: False,
                    executable="/usr/bin/python3",
                )
            self.assertEqual(code, 0, stdout.getvalue())
            self.assertEqual((app / ".env.local").read_text(encoding="utf-8"), secret)
            self.assertEqual(os.stat(app / ".env.local").st_mode & 0o777, 0o600)

    def test_download_is_pinned_to_the_checked_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            app = self._install(home, revision=OLD_SHA)
            source = home / "incoming"
            source.mkdir()
            (source / "launch.py").write_text("print('new')\n", encoding="utf-8")
            (source / "VERSION").write_text("1.0.5\n", encoding="utf-8")
            calls: list[str] = []

            def fetch(url: str) -> bytes:
                calls.append(url)
                if _github_api(url):
                    return json.dumps({"sha": NEW_SHA}).encode()
                return _archive(source)

            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                code = updater.run_update(
                    app,
                    fetch=fetch,
                    home=home,
                    path_env="",
                    shell="/bin/bash",
                    server_running=lambda: False,
                    executable="/usr/bin/python3",
                )
            self.assertEqual(code, 0, stdout.getvalue())
            self.assertIn(
                f"https://github.com/Viperisuseful/ViperCapture/archive/{NEW_SHA}.tar.gz",
                calls,
            )
            self.assertFalse(any("refs/heads" in url for url in calls))

    def test_windows_update_refuses_to_rename_a_running_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            app = self._install(home, revision=OLD_SHA)
            source = home / "incoming"
            source.mkdir()
            (source / "launch.py").write_text("print('new')\n", encoding="utf-8")
            (source / "VERSION").write_text("1.0.5\n", encoding="utf-8")
            stderr = io.StringIO()
            with mock.patch("sys.stderr", stderr):
                code = updater.run_update(
                    app,
                    fetch=self._fetch(_archive(source)),
                    home=home,
                    path_env="",
                    platform_name="win32",
                    shell="",
                    server_running=lambda: True,
                    executable=r"C:\Python\python.exe",
                )
            self.assertEqual(code, 1)
            self.assertIn("still running", stderr.getvalue())
            self.assertIn("Stop it", stderr.getvalue())
            self.assertEqual((app / "launch.py").read_text(encoding="utf-8"), "print('old')\n")
            self.assertFalse((home / ".vipercapture" / "app.updating").exists())
            self.assertFalse((home / ".vipercapture" / "app.backup").exists())

    def test_windows_shim_uses_the_console_code_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vipercapture.cmd"
            python = "C:\\Users\\José\\python.exe"
            launch_path = Path("C:/Users/José/ViperCapture/app/launch.py")
            updater.write_shim(path, python, launch_path, "win32", encoding="cp1252")
            raw = path.read_bytes()
            self.assertIn("José".encode("cp1252"), raw)
            self.assertNotIn("José".encode("utf-8"), raw)
            self.assertTrue(raw.startswith(b"@echo off\r\n"))
            with self.assertRaises(updater.UpdateError):
                updater.write_shim(
                    path,
                    "C:\\Users\\你好\\python.exe",
                    Path("C:/Users/你好/app/launch.py"),
                    "win32",
                    encoding="cp1252",
                )

    def test_windows_shim_uses_cmd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            app = self._install(home)
            bin_dir = home / "bin"
            bin_dir.mkdir()
            shim = bin_dir / "vipercapture.cmd"
            shim.write_text(
                f'@echo off\r\n"C:\\Python\\python.exe" "{app / "launch.py"}" %*\r\n',
                encoding="utf-8",
            )
            source = home / "incoming"
            source.mkdir()
            (source / "launch.py").write_text("print('new')\n", encoding="utf-8")
            (source / "VERSION").write_text("1.0.5\n", encoding="utf-8")
            code = updater.run_update(
                app,
                fetch=self._fetch(_archive(source)),
                home=home,
                path_env=str(bin_dir),
                platform_name="win32",
                shell="",
                server_running=lambda: False,
                executable="C:\\Python\\python.exe",
            )
            self.assertEqual(code, 0)
            raw = shim.read_bytes()
            self.assertTrue(raw.startswith(b"@echo off\r\n"))
            self.assertIn(b'"C:\\Python\\python.exe"', raw)
            self.assertIn(b"%*", raw)
            self.assertNotIn(b"#!/bin/sh", raw)

    def test_new_posix_shim_records_path_without_trusting_the_live_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            app = self._install(home)
            source = home / "incoming"
            source.mkdir()
            (source / "launch.py").write_text("print('new')\n", encoding="utf-8")
            (source / "VERSION").write_text("1.0.4\n", encoding="utf-8")
            bin_dir = home / ".local" / "bin"
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                code = updater.run_update(
                    app,
                    fetch=self._fetch(_archive(source)),
                    home=home,
                    path_env=str(bin_dir),
                    shell="/bin/bash",
                    server_running=lambda: False,
                    executable="/usr/bin/python3",
                )
            self.assertEqual(code, 0, stdout.getvalue())
            shim = bin_dir / "vipercapture"
            self.assertTrue(os.access(shim, os.X_OK))
            bashrc = (home / ".bashrc").read_text(encoding="utf-8")
            self.assertEqual(bashrc.count("# ViperCapture"), 1)
            self.assertIn(f'export PATH="{bin_dir}:$PATH"', bashrc)
            self.assertIn("Open a new terminal", stdout.getvalue())

    def test_refuses_a_source_checkout_without_downloading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / "launch.py").write_text("print('checkout')\n", encoding="utf-8")
            calls: list[str] = []
            stderr = io.StringIO()
            with mock.patch("sys.stderr", stderr):
                code = updater.run_update(
                    root,
                    fetch=lambda url: calls.append(url) or b"",
                    home=root,
                    path_env="",
                    server_running=lambda: False,
                )
            self.assertEqual(code, 1)
            self.assertEqual(calls, [])
            self.assertIn("source checkout", stderr.getvalue())
            self.assertEqual((root / "launch.py").read_text(encoding="utf-8"), "print('checkout')\n")

    def test_unsafe_archive_leaves_the_install_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            app = self._install(home, revision=OLD_SHA)
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
                info = tarfile.TarInfo("../escape.txt")
                payload = b"nope\n"
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
            stderr = io.StringIO()
            with mock.patch("sys.stderr", stderr):
                code = updater.run_update(
                    app,
                    fetch=self._fetch(buffer.getvalue()),
                    home=home,
                    path_env="",
                    server_running=lambda: False,
                    executable="/usr/bin/python3",
                )
            self.assertEqual(code, 1)
            self.assertIn("unsafe path", stderr.getvalue())
            self.assertEqual((app / "launch.py").read_text(encoding="utf-8"), "print('old')\n")
            self.assertFalse((home / "escape.txt").exists())

    def test_http_archive_url_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            app = self._install(home)
            stderr = io.StringIO()
            with mock.patch("sys.stderr", stderr):
                code = updater.run_update(
                    app,
                    fetch=self._fetch(b"unused"),
                    archive_url="http://example.com/ViperCapture.tar.gz",
                    home=home,
                    path_env="",
                    server_running=lambda: False,
                    executable="/usr/bin/python3",
                )
            self.assertEqual(code, 1)
            self.assertIn("Refusing non-HTTPS URL", stderr.getvalue())

    def test_recovers_a_venv_left_in_the_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            app = self._install(home)
            marker = app / ".venv" / "marker"
            backup = home / ".vipercapture" / "app.backup"
            marker.unlink()
            (app / ".venv").rmdir()
            backup.mkdir()
            shutil_venv = backup / ".venv"
            shutil_venv.mkdir()
            (shutil_venv / "marker").write_text("keep\n", encoding="utf-8")
            updater._recover_interrupted_update(app, backup)
            self.assertEqual((app / ".venv" / "marker").read_text(encoding="utf-8"), "keep\n")
            self.assertFalse(backup.exists())

    def test_download_limit_and_windows_path_merge(self) -> None:
        def read(size: int) -> bytes:
            return b"x" * size

        with self.assertRaises(updater.UpdateError):
            updater.consume_limited(read, 10)
        self.assertIsNone(updater.merge_windows_path(r"C:\already", r"C:\already"))
        self.assertEqual(
            updater.merge_windows_path(r"C:\Windows", r"C:\Users\viper\AppData\Local\ViperCapture\bin"),
            r"C:\Users\viper\AppData\Local\ViperCapture\bin;C:\Windows",
        )
        self.assertEqual(updater.escape_posix_path('/tmp/we"ird$`'), '/tmp/we\\"ird\\$\\`')

    def test_symlink_in_the_archive_is_not_extracted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            outside = home / "secret"
            outside.write_text("hidden\n", encoding="utf-8")
            source = home / "incoming"
            source.mkdir()
            (source / "launch.py").write_text("print('new')\n", encoding="utf-8")
            (source / "VERSION").write_text("1.0.5\n", encoding="utf-8")
            link = source / "linked"
            link.symlink_to(outside)
            app = self._install(home)
            code = updater.run_update(
                app,
                fetch=self._fetch(_archive(source)),
                home=home,
                path_env=str(home / "bin"),
                shell="/bin/bash",
                server_running=lambda: False,
                executable="/usr/bin/python3",
            )
            self.assertEqual(code, 0)
            self.assertFalse((app / "linked").exists())


if __name__ == "__main__":
    unittest.main()
