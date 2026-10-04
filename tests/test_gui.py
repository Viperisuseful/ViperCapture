from __future__ import annotations

import io
import json
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

import launch
import gui


class GuiKeyTests(TestCase):
    def test_tab_cycles_format_and_ctrl_p_switches_the_page(self) -> None:
        state = gui.GuiState()
        state, action = gui.apply_key(state, "tab")
        self.assertEqual(action, "edit")
        self.assertEqual(state.output, "gif")
        state, _action = gui.apply_key(state, "tab")
        self.assertEqual(state.output, "mp4")
        state, _action = gui.apply_key(state, "tab")
        self.assertEqual(state.output, "png")
        state, _action = gui.apply_key(state, "ctrl-p")
        self.assertTrue(state.full_page)
        state, _action = gui.apply_key(state, "ctrl-p")
        self.assertFalse(state.full_page)

    def test_enter_without_a_link_stays_on_the_form(self) -> None:
        state, action = gui.apply_key(gui.GuiState(), "enter")
        self.assertEqual(action, "edit")
        self.assertIn("website link", state.error)

    def test_typing_backspace_and_escape(self) -> None:
        state = gui.GuiState()
        for key in "example.com":
            state, action = gui.apply_key(state, key)
            self.assertEqual(action, "edit")
        self.assertEqual(state.url, "example.com")
        state, _action = gui.apply_key(state, "backspace")
        self.assertEqual(state.url, "example.co")
        state, action = gui.apply_key(state, "esc")
        self.assertEqual(action, "quit")

    def test_arrow_keys_are_not_typed_into_the_link(self) -> None:
        self.assertEqual(gui.keys_from_bytes(bytearray(), b"ab\x1b[A\t"), ["a", "b", "tab"])
        pending = bytearray(b"\x1b")
        self.assertEqual(gui.keys_from_bytes(pending, b""), [])
        self.assertEqual(gui.keys_from_bytes(pending, b"[B"), [])
        self.assertEqual(pending, bytearray())

    def test_a_lone_escape_flushes_after_the_input_wait(self) -> None:
        pending = bytearray(b"\x1b")
        self.assertEqual(gui.keys_from_bytes(pending, b""), [])
        self.assertEqual(gui._input_timeout(pending, capturing=False), 0.05)
        self.assertIsNone(gui._input_timeout(bytearray(), capturing=False))
        self.assertEqual(gui.flush_bare_escape(pending), ["esc"])
        self.assertEqual(pending, bytearray())
        partial = bytearray(b"\x1b[")
        self.assertEqual(gui.flush_bare_escape(partial), [])
        self.assertEqual(partial, bytearray(b"\x1b["))

    def test_unicode_url_bytes_stay_in_the_link(self) -> None:
        pending = bytearray()
        self.assertEqual(gui.keys_from_bytes(pending, "münich".encode()), ["m", "ü", "n", "i", "c", "h"])
        split = bytearray()
        self.assertEqual(gui.keys_from_bytes(split, b"\xc3"), [])
        self.assertEqual(gui.keys_from_bytes(split, b"\xbc"), ["ü"])
        state = gui.GuiState()
        for key in gui.keys_from_bytes(bytearray(), "münich.example".encode()):
            state, action = gui.apply_key(state, key)
            self.assertEqual(action, "edit")
        self.assertEqual(state.url, "münich.example")


class GuiRequestTests(TestCase):
    def test_viewport_capture_defaults_and_video_does_not_scroll(self) -> None:
        state = gui.GuiState(url="example.com", output="gif", viewport=(1920, 1080))
        payload = gui.capture_payload(state, gui.normalize_url(state.url))
        self.assertEqual(payload["url"], "https://example.com")
        self.assertEqual(payload["output"], "gif")
        self.assertFalse(payload["full_page"])
        self.assertEqual(payload["viewport"], {"width": 1920, "height": 1080})
        self.assertEqual(payload["video"], {"duration_ms": 4000, "scroll": False})

    def test_a_link_without_a_scheme_uses_https(self) -> None:
        self.assertEqual(gui.normalize_url("example.com/docs"), "https://example.com/docs")
        self.assertEqual(gui.normalize_url("localhost:8080"), "https://localhost:8080")
        with self.assertRaises(ValueError):
            gui.normalize_url("javascript:alert(1)")

    def test_full_page_video_scrolls_by_the_page_flag(self) -> None:
        state = gui.GuiState(url="https://example.com", output="mp4", full_page=True)
        payload = gui.capture_payload(state, state.url)
        self.assertTrue(payload["full_page"])
        self.assertEqual(payload["video"], {"duration_ms": 4000, "scroll": False})

    def test_full_page_png_has_no_video_settings(self) -> None:
        state = gui.GuiState(url="https://example.com/docs", full_page=True)
        payload = gui.capture_payload(state, state.url)
        self.assertTrue(payload["full_page"])
        self.assertNotIn("video", payload)
        self.assertEqual(payload["output"], "png")

    def test_api_error_message_is_shown_without_the_json_envelope(self) -> None:
        body = json.dumps(
            {"error": {"code": "navigation_failed", "message": "The site refused the connection."}}
        ).encode()
        self.assertEqual(gui.error_text(502, body), "The site refused the connection.")

    def test_capture_saves_a_png_and_reports_a_bad_file(self) -> None:
        moment = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
        png = b"\x89PNG\r\n\x1a\n" + b"image"

        class Response:
            def __init__(self, body: bytes) -> None:
                self.body = body
                self.sent = False

            def read(self, _size: int) -> bytes:
                if self.sent:
                    return b""
                self.sent = True
                return self.body

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_args: object) -> bool:
                return False

        with TemporaryDirectory() as tmp:
            state = gui.perform_capture(
                gui.GuiState(url="https://example.com", busy=True),
                directory=Path(tmp),
                moment=moment,
                opener=lambda _request, timeout: Response(png),
            )
            self.assertFalse(state.busy)
            self.assertTrue(state.saved.endswith(".png"))
            self.assertTrue(Path(state.saved).read_bytes().startswith(b"\x89PNG"))
            rejected = gui.perform_capture(
                gui.GuiState(url="https://example.com", output="gif", busy=True),
                directory=Path(tmp),
                moment=moment,
                opener=lambda _request, timeout: Response(png),
            )
            self.assertIn("did not return a gif", rejected.error)
            before = set(Path(tmp).iterdir())
            cancel = gui.CancelFlag()
            cancel.requested = True
            cancelled = gui.perform_capture(
                gui.GuiState(url="https://example.com", busy=True),
                directory=Path(tmp),
                moment=moment,
                opener=lambda _request, timeout: Response(png),
                cancel=cancel,
            )
            self.assertEqual(cancelled.error, "Capture cancelled.")
            self.assertFalse(cancelled.saved)
            self.assertEqual(set(Path(tmp).iterdir()), before)

    def test_capture_sends_the_configured_bearer_token(self) -> None:
        seen: dict[str, str] = {}

        def opener(request: object, timeout: float) -> object:
            del timeout
            seen["authorization"] = request.get_header("Authorization")  # type: ignore[attr-defined]
            return _png_response(b"\x89PNG\r\n\x1a\n")

        admin = "gui-admin-token-0123456789abcdef"
        project = "gui-project-key"
        with TemporaryDirectory() as tmp:
            gui.perform_capture(
                gui.GuiState(url="https://example.com", busy=True),
                directory=Path(tmp),
                opener=opener,
                started_server=True,
                authorization=admin,
            )
            self.assertEqual(seen["authorization"], f"Bearer {admin}")
            gui.perform_capture(
                gui.GuiState(url="https://example.com", busy=True),
                directory=Path(tmp),
                opener=opener,
                started_server=False,
                authorization=project,
            )
            self.assertEqual(seen["authorization"], f"Bearer {project}")
        self.assertEqual(
            gui.capture_credential(
                started_server=True,
                environ={
                    "VIPERCAPTURE_ADMIN_TOKEN": admin,
                    "VIPERCAPTURE_API_KEY": project,
                },
            ),
            admin,
        )
        self.assertEqual(
            gui.capture_credential(
                started_server=False,
                environ={
                    "VIPERCAPTURE_ADMIN_TOKEN": admin,
                    "VIPERCAPTURE_API_KEY": project,
                },
            ),
            project,
        )
        self.assertEqual(
            gui.capture_credential(started_server=False, environ={}),
            "",
        )

    def test_a_planted_part_symlink_is_not_followed(self) -> None:
        moment = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
        png = b"\x89PNG\r\n\x1a\n" + b"image"
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            victim = directory / "victim"
            victim.write_text("safe", encoding="utf-8")
            final = gui.output_path(directory, "https://example.com", "png", moment)
            final.with_suffix(final.suffix + ".part").symlink_to(victim)
            state = gui.perform_capture(
                gui.GuiState(url="https://example.com", busy=True),
                directory=directory,
                moment=moment,
                opener=lambda _request, timeout: _png_response(png),
            )
            self.assertTrue(state.saved.endswith(".png"))
            self.assertEqual(Path(state.saved).read_bytes(), png)
            self.assertEqual(victim.read_text(encoding="utf-8"), "safe")
            self.assertFalse(any(path.name.startswith(".vipercapture-") for path in directory.iterdir()))

    def test_cancel_closes_the_open_request(self) -> None:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        server.settimeout(2)
        port = server.getsockname()[1]
        accepted: list[socket.socket] = []

        def hold() -> None:
            try:
                conn, _addr = server.accept()
            except OSError:
                return
            accepted.append(conn)
            conn.settimeout(2)
            try:
                while conn.recv(4096):
                    pass
            except OSError:
                return

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        cancel = gui.CancelFlag()
        result: dict[str, gui.GuiState] = {}

        def work() -> None:
            with TemporaryDirectory() as tmp:
                result["state"] = gui.perform_capture(
                    gui.GuiState(url="https://example.com", busy=True),
                    directory=Path(tmp),
                    endpoint=f"http://127.0.0.1:{port}/v1/render",
                    timeout=5,
                    cancel=cancel,
                    authorization="",
                )

        worker = threading.Thread(target=work)
        worker.start()
        deadline = time.monotonic() + 2
        while not accepted and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(accepted)
        cancel.abort()
        worker.join(2)
        server.close()
        for conn in accepted:
            conn.close()
        self.assertFalse(worker.is_alive())
        self.assertEqual(result["state"].error, "Capture cancelled.")
        self.assertFalse(result["state"].saved)


class GuiFrameTests(TestCase):
    def test_plain_terminal_uses_the_ascii_wordmark_and_no_log_window(self) -> None:
        frame = gui.render_frame(launch.WindowSize(100, 40), gui.GuiState())
        text = gui.visible(frame.decode("utf-8", errors="replace"))
        self.assertIn(gui.ASCII_LOGO[0], text)
        self.assertIn("ViperCapture", text)
        self.assertIn("example.com", text)
        self.assertIn("png", text)
        self.assertIn("viewport", text)
        self.assertIn("1920x1080", text)
        self.assertIn("tab format", text)
        self.assertIn("ctrl+p page", text)
        self.assertNotIn("Requests:", text)
        self.assertNotIn("\x1b_G", frame.decode("utf-8", errors="replace"))

    def test_capture_frame_has_a_progress_bar(self) -> None:
        state = gui.GuiState(url="https://example.com", busy=True, progress=4)
        text = gui.visible(
            gui.render_frame(launch.WindowSize(100, 40), state).decode("utf-8", errors="replace")
        )
        self.assertIn("Capturing", text)
        self.assertIn("█", text)
        self.assertIn("░", text)
        self.assertIn("esc cancel", text)
        starting = gui.visible(
            gui.render_frame(
                launch.WindowSize(100, 40),
                gui.GuiState(busy=True, progress=2),
            ).decode("utf-8", errors="replace")
        )
        self.assertIn("Starting", starting)
        self.assertNotIn("Capturing", starting)

    def test_error_is_in_the_frame(self) -> None:
        state = gui.GuiState(url="https://example.com", error="The site refused the connection.")
        text = gui.visible(
            gui.render_frame(launch.WindowSize(80, 30), state).decode("utf-8", errors="replace")
        )
        self.assertIn("The site refused the connection.", text)

    def test_progress_redraw_leaves_the_logo_where_it_is(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"x"
        size = launch.WindowSize(100, 40)
        first = gui.render_frame(size, gui.GuiState(), protocol="kitty", png=png)
        self.assertIn(b"\x1b_G", first)
        self.assertNotIn(gui.ASCII_LOGO[0].encode(), first)
        logo_rows = min(6, max(1, 40 // 5))
        origin = gui._frame_origin(40, len(gui._frame_block(gui.GuiState(), 100, image=True)), logo_rows)
        second = gui.render_frame(
            size,
            gui.GuiState(url="https://example.com", busy=True, progress=3),
            clear=False,
            logo=b"",
            logo_rows=logo_rows,
            origin=origin,
        )
        self.assertNotIn(b"\x1b[2J", second)
        self.assertNotIn(b"\x1b_G", second)
        self.assertIn(b"Capturing", second)

    def test_logo_is_sent_once_until_the_layout_changes(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"x"
        out = io.BytesIO()
        stdout = mock.Mock()
        stdout.buffer = out
        drawer = gui._Drawer()
        with (
            mock.patch.object(gui, "_window_size", return_value=launch.WindowSize(100, 40)),
            mock.patch.object(gui.launch, "choose_graphics_protocol", return_value="kitty"),
            mock.patch.object(gui.launch, "_logo_png", return_value=png),
            mock.patch.object(gui.sys, "stdout", stdout),
        ):
            drawer.draw(gui.GuiState())
            drawer.draw(gui.GuiState(url="https://news.example/story"))
        sent = out.getvalue()
        self.assertEqual(sent.count(b"a=T,f=100"), 1)
        self.assertIn(b"https://news.example/story", sent)

    def test_a_dead_server_is_reported_without_waiting_out_the_clock(self) -> None:
        class Server:
            def poll(self) -> int:
                return 1

        with (
            mock.patch.object(gui.launch, "port_open", return_value=False),
            mock.patch.object(gui.time, "sleep"),
            mock.patch.object(gui._Drawer, "draw"),
        ):
            state = gui._wait_until_ready(
                gui.GuiState(),
                gui.time.monotonic() + 30,
                Server(),  # type: ignore[arg-type]
                gui._Drawer(),
            )
        self.assertIn("did not start", state.error)
        self.assertFalse(state.busy)

    def test_server_command_does_not_open_another_terminal(self) -> None:
        command = " ".join(gui.server_command())
        self.assertIn("uvicorn", command)
        self.assertIn("127.0.0.1", command)
        self.assertNotIn("ghostty", command)
        self.assertNotIn("kitty", command)
        self.assertNotIn("tail", command)


def _png_response(body: bytes) -> object:
    class Response:
        def __init__(self) -> None:
            self.body = body
            self.sent = False

        def read(self, _size: int) -> bytes:
            if self.sent:
                return b""
            self.sent = True
            return self.body

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

    return Response()


class GuiRuntimeTests(TestCase):
    def test_windows_size_does_not_use_posix_terminal_calls(self) -> None:
        with (
            mock.patch.object(gui.os, "name", "nt"),
            mock.patch.object(
                gui.shutil,
                "get_terminal_size",
                return_value=os_terminal_size(100, 40),
            ),
            mock.patch.object(
                gui.launch,
                "read_window_size",
                side_effect=AssertionError("posix"),
            ),
        ):
            size = gui._window_size()
        self.assertEqual((size.cols, size.rows), (100, 40))

    def test_missing_termios_falls_back_to_the_stdlib_size(self) -> None:
        with (
            mock.patch.object(gui.os, "name", "posix"),
            mock.patch.object(
                gui.launch,
                "read_window_size",
                side_effect=ModuleNotFoundError("termios"),
            ),
            mock.patch.object(
                gui.shutil,
                "get_terminal_size",
                return_value=os_terminal_size(90, 30),
            ),
        ):
            size = gui._window_size()
        self.assertEqual((size.cols, size.rows), (90, 30))

    def test_dependency_hashes_are_checked_before_the_screen(self) -> None:
        order: list[str] = []
        with (
            mock.patch.object(gui.sys.stdin, "isatty", return_value=True),
            mock.patch.object(gui.sys.stdout, "isatty", return_value=True),
            mock.patch.object(gui.launch, "ensure_deps", side_effect=lambda: order.append("deps")),
            mock.patch.object(
                gui.launch,
                "ensure_playwright",
                side_effect=lambda: order.append("browsers"),
            ),
            mock.patch.object(gui.launch, "port_open", return_value=True),
            mock.patch.object(gui.launch, "choose_graphics_protocol", return_value=None),
            mock.patch.object(gui, "_enter", side_effect=lambda: order.append("enter")),
            mock.patch.object(gui, "_leave"),
            mock.patch.object(gui, "_run_keys", return_value=0),
        ):
            code = gui.run_capture_gui((1920, 1080), directory=Path("."))
        self.assertEqual(code, 0)
        self.assertEqual(order, ["deps", "browsers", "enter"])


def os_terminal_size(columns: int, lines: int) -> object:
    import os

    return os.terminal_size((columns, lines))


class GuiHelpTests(TestCase):
    def test_help_mentions_the_menu(self) -> None:
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            with self.assertRaises(SystemExit) as caught:
                launch.parse_cli(["--help"])
        self.assertEqual(caught.exception.code, 0)
        text = stdout.getvalue()
        self.assertIn("--gui", text)
        self.assertIn("1920 1080", text)
        self.assertIn("Ctrl+P", text)
