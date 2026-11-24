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
        for expected in gui.FORMATS[1:]:
            state, action = gui.apply_key(state, "tab")
            self.assertEqual(action, "edit")
            self.assertEqual(state.output, expected)
        state, _action = gui.apply_key(state, "tab")
        self.assertEqual(state.output, gui.FORMATS[0])
        state, _action = gui.apply_key(state, "shift-tab")
        self.assertEqual(state.output, gui.FORMATS[-1])
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
        self.assertEqual(gui.keys_from_bytes(bytearray(), b"ab\x1b[A\t"), ["a", "b", "up", "tab"])
        pending = bytearray(b"\x1b")
        self.assertEqual(gui.keys_from_bytes(pending, b""), [])
        self.assertEqual(gui.keys_from_bytes(pending, b"[B"), ["down"])
        self.assertEqual(pending, bytearray())
        state = gui.GuiState(url="ab")
        for key in ("up", "down", "left", "right"):
            state, _ = gui.apply_key(state, key)
        self.assertEqual(state.url, "ab")

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
            try:
                final.with_suffix(final.suffix + ".part").symlink_to(victim)
            except OSError:
                self.skipTest("symlinks require elevated privileges or Developer Mode on Windows")
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
        logo_cols, logo_rows = launch.logo_cells(size)
        origin = gui._frame_origin(40, len(gui._frame_block(gui.GuiState(), 100, image=True)), logo_rows)
        second = gui.render_frame(
            size,
            gui.GuiState(url="https://example.com", busy=True, progress=3),
            clear=False,
            logo=b"",
            logo_cols=logo_cols,
            logo_rows=logo_rows,
            origin=origin,
        )
        self.assertNotIn(b"\x1b[2J", second)
        self.assertNotIn(b"\x1b_G", second)
        self.assertIn(b"Capturing", second)

    def test_menu_logo_uses_a_square_cell_box(self) -> None:
        size = launch.WindowSize(120, 40, 1200, 800)
        png = b"\x89PNG\r\n\x1a\n"
        frame = gui.render_frame(size, gui.GuiState(), protocol="kitty", png=png)
        logo_cols, logo_rows = launch.logo_cells(size)
        self.assertGreater(logo_cols, logo_rows)
        self.assertIn(f"c={logo_cols},r={logo_rows}".encode(), frame)
        self.assertNotIn(b"c=24,r=", frame)
        cell_w = size.xpixels / size.cols
        cell_h = size.ypixels / size.rows
        self.assertLess(abs(logo_cols * cell_w - logo_rows * cell_h), cell_w + cell_h)
        col = max(1, (size.cols - logo_cols) // 2 + 1)
        self.assertIn(f";{col}H".encode(), frame)

    def test_narrow_terminal_skips_the_stretched_logo(self) -> None:
        size = launch.WindowSize(20, 8, 200, 160)
        frame = gui.render_frame(size, gui.GuiState(), protocol="kitty", png=b"\x89PNG\r\n\x1a\n")
        self.assertNotIn(b"\x1b_G", frame)
        self.assertIn(b"ViperCapture", frame)

    def test_opencode_styled_ascii_wordmark(self) -> None:
        lines = gui.styled_ascii_logo()
        self.assertEqual(len(lines), 5)
        self.assertEqual(gui.visible(lines[0]), gui.ASCII_LOGO[0])
        self.assertEqual(gui.visible(lines[4]), gui.ASCII_LOGO[4])
        self.assertIn('ViperCapture', gui.visible(lines[4]))
        self.assertIn("\x1b[", lines[0])
        self.assertIn("\x1b[1;34m", lines[1])
        self.assertIn("\x1b[1;37m", lines[1])
        self.assertNotIn("\x1b[90m", lines[1])
        self.assertIn("\x1b[48;2;60;66;80m", lines[2])
        self.assertIn("\x1b[48;2;20;42;90m", lines[2])

    def test_wordmark_switches_between_plain_and_styled_by_width(self) -> None:
        self.assertEqual(gui._wordmark(40, image=False), ("ViperCapture",))
        self.assertEqual(gui._wordmark(60, image=False), ("ViperCapture",))
        self.assertEqual(gui._wordmark(63, image=False), ("ViperCapture",))
        self.assertEqual(gui._wordmark(64, image=False), tuple(gui.styled_ascii_logo()))
        self.assertEqual(gui._wordmark(80, image=False), tuple(gui.styled_ascii_logo()))
        self.assertEqual(gui._wordmark(80, image=True), ())
        self.assertEqual(gui._wordmark(80, image=False, rows=12), ("ViperCapture",))

    def test_narrow_and_short_terminals_never_overflow_or_clip_panel(self) -> None:
        import re

        csi_move = re.compile(r"\x1b\[(\d+);(\d+)H")
        sizes = [
            launch.WindowSize(120, 30),
            launch.WindowSize(80, 24),
            launch.WindowSize(50, 18),
            launch.WindowSize(40, 14),
            launch.WindowSize(120, 12),
            launch.WindowSize(80, 10),
        ]
        states = [
            gui.GuiState(),
            gui.GuiState(
                url="https://example.com/a/very/long/path/that/exceeds/the/panel/width/1234567890Z"
            ),
            gui.GuiState(
                url="https://example.com",
                output="mp4",
                full_page=True,
                viewport=(3840, 2160),
                error="A full FFmpeg executable is unavailable. Install FFmpeg on PATH or set VIPERCAPTURE_FFMPEG.",
            ),
            gui.GuiState(url="https://example.com", busy=True, progress=7),
            gui.GuiState(
                url="https://example.com",
                saved=r"C:\Users\viper\Documents\ViperCapture\vipercapture-example.com-20261121.png",
            ),
        ]
        for size in sizes:
            for state in states:
                frame = gui.render_frame(size, state).decode("utf-8", errors="replace")
                parts = csi_move.split(frame)
                placed = []
                for idx in range(1, len(parts), 3):
                    row, col = int(parts[idx]), int(parts[idx + 1])
                    vis = gui.visible(parts[idx + 2])
                    placed.append((row, col, vis))
                    self.assertLessEqual(
                        col + gui._display_width(vis) - 1,
                        size.cols,
                        f"Horizontal overflow at {size.cols}x{size.rows}: {vis!r}",
                    )
                    self.assertLessEqual(row, size.rows)
                lines = [vis for _, _, vis in placed]
                self.assertTrue(any("└" in line and "┘" in line for line in lines))
                self.assertTrue(any("esc" in line for line in lines))
                if state.url.endswith("Z"):
                    self.assertTrue(
                        any("Z" in line for line in lines),
                        f"Last typed char truncated at {size.cols}x{size.rows}",
                    )

    def test_wide_cjk_urls_and_below_minimum_terminals_stay_within_physical_bounds(self) -> None:
        import re

        csi_move = re.compile(r"\x1b\[(\d+);(\d+)H")
        cjk_state = gui.GuiState(
            url="https://example.com/日本語テスト/長いパス/確認用ページ最終Z",
            error="エラー: 日本語の長いメッセージでも右枠を超えないこと",
        )
        for size in (
            launch.WindowSize(80, 24),
            launch.WindowSize(40, 14),
            launch.WindowSize(16, 6),
            launch.WindowSize(10, 5),
        ):
            frame = gui.render_frame(size, cjk_state).decode("utf-8", errors="replace")
            parts = csi_move.split(frame)
            for idx in range(1, len(parts), 3):
                row, col = int(parts[idx]), int(parts[idx + 1])
                vis = gui.visible(parts[idx + 2])
                self.assertLessEqual(row, size.rows)
                self.assertLessEqual(
                    col + gui._display_width(vis) - 1,
                    size.cols,
                    f"Cell overflow at {size.cols}x{size.rows}: {vis!r}",
                )

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

    def test_windows_drawer_places_the_inline_image_with_the_cursor_visible(self) -> None:
        png = launch.LOGO_PNG.read_bytes()
        size = launch.WindowSize(100, 40)
        output = io.BytesIO()
        stdout = mock.Mock(buffer=output)
        drawer = gui._Drawer()
        with (
            mock.patch.object(gui.os, "name", "nt"),
            mock.patch.object(gui, "_window_size", return_value=size),
            mock.patch.object(launch, "choose_graphics_protocol", return_value="iterm2") as choose,
            mock.patch.object(launch, "_logo_png", return_value=png),
            mock.patch.object(gui.sys, "stdout", stdout),
        ):
            drawer.draw(gui.GuiState())
            initial = output.getvalue()
            drawer.draw(gui.GuiState(url="example.org"))

        cols, rows = launch.logo_cells(size)
        col = (size.cols - cols) // 2 + 1
        block_len = len(gui._frame_block(gui.GuiState(), size.cols, image=True, rows=size.rows - rows - 1))
        row = (size.rows - block_len - rows) // 2
        image = launch.iterm2_image_sequence(png, width=cols, height=rows)
        choose.assert_called_with(alternate_screen=True)
        self.assertIn(b"enter capture", initial)
        self.assertTrue(initial.endswith(f"\x1b[?25h\x1b[{row};{col}H".encode() + image + b"\x1b[?25l"))
        self.assertIn(b"example.org", output.getvalue()[len(initial):])
        self.assertEqual(output.getvalue().count(b"\x1b]1337;File="), 1)

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

    def test_window_size_handles_stream_without_fileno_gracefully(self) -> None:
        broken_stream = mock.Mock()
        broken_stream.fileno.side_effect = ValueError("I/O operation on closed file")
        with (
            mock.patch.object(gui.os, "name", "posix"),
            mock.patch.object(gui.launch, "read_window_size", side_effect=OSError("not tty")),
            mock.patch.object(gui.sys, "stdout", broken_stream),
            mock.patch.object(gui.sys, "stdin", broken_stream),
            mock.patch.object(
                gui.shutil,
                "get_terminal_size",
                return_value=os_terminal_size(88, 22),
            ),
        ):
            size = gui._window_size()
        self.assertEqual((size.cols, size.rows), (88, 22))

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

    def test_windows_vt_mode_is_enabled_before_the_screen_and_restored(self) -> None:
        order: list[object] = []

        def enable() -> int:
            order.append("enable")
            return 7

        def restore(previous: int | None) -> None:
            order.append(("restore", previous))

        with (
            mock.patch.object(gui.sys.stdin, "isatty", return_value=True),
            mock.patch.object(gui.sys.stdout, "isatty", return_value=True),
            mock.patch.object(gui.launch, "ensure_deps"),
            mock.patch.object(gui.launch, "ensure_playwright"),
            mock.patch.object(gui.launch, "port_open", return_value=True),
            mock.patch.object(gui.launch, "choose_graphics_protocol", return_value=None) as choose,
            mock.patch.object(gui, "_enable_windows_vt", side_effect=enable),
            mock.patch.object(gui, "_restore_windows_vt", side_effect=restore),
            mock.patch.object(gui, "_enter", side_effect=lambda: order.append("enter")),
            mock.patch.object(gui, "_leave", side_effect=lambda _protocol: order.append("leave")),
            mock.patch.object(gui, "_run_keys", return_value=0),
        ):
            code = gui.run_capture_gui((1920, 1080), directory=Path("."))
        choose.assert_called_once_with(alternate_screen=True)
        self.assertEqual(code, 0)
        self.assertEqual(order, ["enable", "enter", "leave", ("restore", 7)])

    def test_windows_vt_mode_sets_the_console_flag_and_can_restore_it(self) -> None:
        import ctypes

        modes: list[int] = []

        class Kernel:
            def GetStdHandle(self, kind: int) -> int:
                self.kind = kind
                return 42

            def GetConsoleMode(self, handle: int, mode: object) -> int:
                self.handle = handle
                mode._obj.value = 0x3  # type: ignore[attr-defined]
                return 1

            def SetConsoleMode(self, handle: int, mode: int) -> int:
                self.handle = handle
                modes.append(mode)
                return 1

        kernel = Kernel()
        windll = mock.Mock()
        windll.kernel32 = kernel
        with (
            mock.patch.object(gui.os, "name", "nt"),
            mock.patch.object(ctypes, "windll", windll, create=True),
        ):
            previous = gui._enable_windows_vt()
            gui._restore_windows_vt(previous)
        self.assertEqual(previous, 0x3)
        self.assertEqual(kernel.kind, -11)
        self.assertEqual(kernel.handle, 42)
        self.assertEqual(modes, [0x3 | 0x0004, 0x3])
        with mock.patch.object(gui.os, "name", "posix"):
            self.assertIsNone(gui._enable_windows_vt())


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
        self.assertIn("--output-dir", text)
        self.assertIn("--server", text)
        self.assertIn("--preset", text)
        self.assertIn("--url", text)
        self.assertIn("F1", text)


class GuiCrossPlatformKeybindTests(TestCase):
    def test_posix_control_and_escape_sequences(self) -> None:
        control_cases = {
            b"\x01": "ctrl-a",
            b"\x02": "ctrl-b",
            b"\x03": "quit",
            b"\x04": "ctrl-d",
            b"\x05": "ctrl-e",
            b"\x06": "ctrl-f",
            b"\x07": "ctrl-g",
            b"\x08": "backspace",
            b"\x09": "tab",
            b"\x0a": "enter",
            b"\x0c": "ctrl-l",
            b"\x0d": "enter",
            b"\x0f": "ctrl-o",
            b"\x10": "ctrl-p",
            b"\x12": "ctrl-r",
            b"\x14": "ctrl-t",
            b"\x15": "ctrl-u",
            b"\x16": "ctrl-v",
            b"\x17": "ctrl-w",
            b"\x18": "ctrl-x",
            b"\x19": "ctrl-y",
            b"\x1f": "ctrl-slash",
            b"\x7f": "backspace",
        }
        for raw, expected in control_cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(gui.keys_from_bytes(bytearray(), raw), [expected])

        escape_cases = {
            b"\x1b[A": "up",
            b"\x1b[B": "down",
            b"\x1b[C": "right",
            b"\x1b[D": "left",
            b"\x1b[H": "home",
            b"\x1b[F": "end",
            b"\x1b[1~": "home",
            b"\x1b[4~": "end",
            b"\x1b[3~": "delete",
            b"\x1b[Z": "shift-tab",
            b"\x1b[1;5C": "ctrl-right",
            b"\x1b[1;5D": "ctrl-left",
            b"\x1bOP": "f1",
            b"\x1b[11~": "f1",
            b"\x1b[47;5u": "ctrl-slash",
            b"\x1bOA": "up",
            b"\x1bOB": "down",
            b"\x1bOC": "right",
            b"\x1bOD": "left",
            b"\x1bOH": "home",
            b"\x1bOF": "end",
        }
        for raw, expected in escape_cases.items():
            with self.subTest(raw=raw):
                pending = bytearray()
                self.assertEqual(gui.keys_from_bytes(pending, raw), [expected])
                self.assertEqual(pending, bytearray())

    def test_windows_control_and_extended_scan_codes(self) -> None:
        for ch, expected in (
            ("\x01", "ctrl-a"),
            ("\x02", "ctrl-b"),
            ("\x03", "quit"),
            ("\x04", "ctrl-d"),
            ("\x05", "ctrl-e"),
            ("\x06", "ctrl-f"),
            ("\x07", "ctrl-g"),
            ("\x08", "backspace"),
            ("\t", "tab"),
            ("\r", "enter"),
            ("\x0c", "ctrl-l"),
            ("\x0f", "ctrl-o"),
            ("\x10", "ctrl-p"),
            ("\x12", "ctrl-r"),
            ("\x14", "ctrl-t"),
            ("\x15", "ctrl-u"),
            ("\x16", "ctrl-v"),
            ("\x17", "ctrl-w"),
            ("\x18", "ctrl-x"),
            ("\x19", "ctrl-y"),
            ("\x1b", "esc"),
            ("\x1f", "ctrl-slash"),
            ("\x7f", "backspace"),
            ("x", "x"),
        ):
            with self.subTest(ch=ch):
                self.assertEqual(gui._windows_key(ch), expected)

        for ext, expected in (
            ("H", "up"),
            ("P", "down"),
            ("K", "left"),
            ("M", "right"),
            ("G", "home"),
            ("O", "end"),
            ("S", "delete"),
            ("s", "ctrl-left"),
            ("t", "ctrl-right"),
            (";", "f1"),
            ("\x0f", "shift-tab"),
        ):
            with self.subTest(ext=ext):
                self.assertEqual(gui._windows_extended_key(ext), expected)

    def test_bracketed_paste_handles_split_chunks_and_does_not_fire_shortcuts(self) -> None:
        pending = bytearray()
        self.assertEqual(gui.keys_from_bytes(pending, b"\x1b[200~https://example.com/"), [])
        self.assertEqual(gui.flush_bare_escape(pending), [])
        keys = gui.keys_from_bytes(pending, b"docs?q=1\x1b[201~")
        self.assertEqual(keys, ["paste:https://example.com/docs?q=1"])
        self.assertEqual(pending, bytearray())

        state = gui.GuiState()
        state, action = gui.apply_key(state, keys[0])
        self.assertEqual(action, "edit")
        self.assertEqual(state.url, "https://example.com/docs?q=1")
        self.assertEqual(state.cursor, len(state.url))
        self.assertEqual(state.drawer, "")

    def test_enter_and_leave_toggle_bracketed_paste_mode(self) -> None:
        out = io.BytesIO()
        stdout = mock.Mock(buffer=out)
        with mock.patch.object(gui.sys, "stdout", stdout):
            gui._enter()
            gui._leave("kitty")
        raw = out.getvalue()
        self.assertIn(gui.BRACKETED_PASTE_ON, raw)
        self.assertIn(gui.BRACKETED_PASTE_OFF, raw)


class GuiUrlEditingAndHistoryTests(TestCase):
    def test_cursor_movement_mid_edit_word_jump_and_delete(self) -> None:
        state = gui.GuiState(url="https://example.com/docs/guide")
        self.assertEqual(state.cursor, len(state.url))

        state, _ = gui.apply_key(state, "home")
        self.assertEqual(state.cursor, 0)
        state, _ = gui.apply_key(state, "end")
        self.assertEqual(state.cursor, len(state.url))

        # Word jump left over 'guide'
        state, _ = gui.apply_key(state, "ctrl-left")
        self.assertEqual(state.url[state.cursor :], "guide")

        # Delete forward ('g')
        state, _ = gui.apply_key(state, "delete")
        self.assertEqual(state.url, "https://example.com/docs/uide")

        # Insert 'G' at cursor
        state, _ = gui.apply_key(state, "G")
        self.assertEqual(state.url, "https://example.com/docs/Guide")

        # Move left 1, backspace removes 'G'
        state, _ = gui.apply_key(state, "left")
        state, _ = gui.apply_key(state, "right")
        state, _ = gui.apply_key(state, "backspace")
        self.assertEqual(state.url, "https://example.com/docs/uide")

        # Ctrl+W deletes previous segment ('docs/')
        state, _ = gui.apply_key(state, "ctrl-w")
        self.assertEqual(state.url, "https://example.com/uide")

        # Ctrl+A / Ctrl+Right / Ctrl+E / Ctrl+U
        state, _ = gui.apply_key(state, "ctrl-a")
        self.assertEqual(state.cursor, 0)
        state, _ = gui.apply_key(state, "ctrl-right")
        self.assertEqual(state.url[state.cursor :], "example.com/uide")
        state, _ = gui.apply_key(state, "ctrl-e")
        self.assertEqual(state.cursor, len(state.url))
        state, _ = gui.apply_key(state, "ctrl-u")
        self.assertEqual(state.url, "")
        self.assertEqual(state.cursor, 0)

    def test_window_around_cursor_scrolls_without_overflow(self) -> None:
        long_url = "https://example.com/one/two/three/four/five/six/seven/eight"
        for pos in (0, 5, 25, len(long_url) - 3, len(long_url)):
            before, caret, after = gui._window_around_cursor(long_url, pos, 24)
            width = gui._display_width(before) + gui._char_width(caret) + gui._display_width(after)
            self.assertLessEqual(width, 24)
            if pos < len(long_url):
                self.assertEqual(caret, long_url[pos])
            else:
                self.assertEqual(caret, " ")

    def test_url_history_cycle_and_persistence(self) -> None:
        state = gui.GuiState(
            url="draft.example",
            url_history=("https://latest.example", "https://older.example"),
        )
        state, _ = gui.apply_key(state, "up")
        self.assertEqual(state.url, "https://latest.example")
        state, _ = gui.apply_key(state, "up")
        self.assertEqual(state.url, "https://older.example")
        state, _ = gui.apply_key(state, "down")
        self.assertEqual(state.url, "https://latest.example")
        state, _ = gui.apply_key(state, "down")
        self.assertEqual(state.url, "draft.example")
        self.assertEqual(state.history_index, -1)

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "gui_history.json"
            urls = [f"https://site-{i}.example" for i in range(60)]
            gui.save_url_history(urls, path=path)
            loaded = gui.load_url_history(path=path)
            self.assertEqual(len(loaded), gui.HISTORY_LIMIT)
            self.assertEqual(loaded[0], "https://site-0.example")
            updated = gui.record_url_history(loaded, "https://site-5.example")
            self.assertEqual(updated[0], "https://site-5.example")
            self.assertEqual(updated.count("https://site-5.example"), 1)


class GuiControlsAndPayloadTests(TestCase):
    def test_viewport_theme_engine_cleanup_and_contextual_duration_controls(self) -> None:
        state = gui.GuiState(viewport=(1440, 900), custom_viewport=(1440, 900))
        # Ctrl+R cycles custom -> Full HD -> HD -> Phone -> Tablet -> 2K -> 4K -> custom
        seen_viewports = [(state.viewport, state.device)]
        for _ in range(len(gui.VIEWPORT_PRESETS) + 1):
            state, _ = gui.apply_key(state, "ctrl-r")
            seen_viewports.append((state.viewport, state.device))
        self.assertIn(((1440, 900), "desktop"), seen_viewports)
        self.assertIn(((390, 844), "iphone_14"), seen_viewports)
        self.assertIn(((768, 1024), "ipad"), seen_viewports)
        self.assertEqual(seen_viewports[0], seen_viewports[-1])

        # Ctrl+T cycles theme
        state, _ = gui.apply_key(state, "ctrl-t")
        self.assertEqual(state.color_scheme, "dark")
        state, _ = gui.apply_key(state, "ctrl-t")
        self.assertEqual(state.color_scheme, "light")
        state, _ = gui.apply_key(state, "ctrl-t")
        self.assertEqual(state.color_scheme, "system")

        # Ctrl+B cycles engine
        state, _ = gui.apply_key(state, "ctrl-b")
        self.assertEqual(state.engine, "firefox")
        state, _ = gui.apply_key(state, "ctrl-b")
        self.assertEqual(state.engine, "webkit")
        state, _ = gui.apply_key(state, "ctrl-b")
        self.assertEqual(state.engine, "chromium")

        # Ctrl+X toggles cleanup
        state, _ = gui.apply_key(state, "ctrl-x")
        self.assertTrue(state.cleanup)
        state, _ = gui.apply_key(state, "ctrl-x")
        self.assertFalse(state.cleanup)

        # Ctrl+D cycles wait delay for non-video and duration for video
        self.assertEqual(state.output, "png")
        state, _ = gui.apply_key(state, "ctrl-d")
        self.assertEqual(state.wait_delay_ms, 1000)
        state, _ = gui.apply_key(state, "ctrl-d")
        self.assertEqual(state.wait_delay_ms, 2500)
        state = gui._replace(state, output="mp4")
        state, _ = gui.apply_key(state, "ctrl-d")
        self.assertEqual(state.video_duration_ms, 8000)
        state, _ = gui.apply_key(state, "ctrl-d")
        self.assertEqual(state.video_duration_ms, 15000)

    def test_capture_payload_all_options_and_contract_validation(self) -> None:
        try:
            from vipercapture.render_contract import RenderRequest
        except ImportError:
            RenderRequest = None

        def validate_contract(doc: dict[str, object]) -> None:
            if RenderRequest is not None:
                RenderRequest.model_validate(doc)

        # Rich image payload
        img_state = gui.GuiState(
            url="https://example.com",
            output="webp",
            full_page=True,
            viewport=(390, 844),
            device="iphone_14",
            color_scheme="dark",
            engine="firefox",
            cleanup=True,
            scale=2,
            quality=80,
            transparent=True,
            profile="accurate",
            wait_delay_ms=2500,
            wait_selector="#ready",
            selector=".hero",
            stealth=False,
            diagnostics=True,
        )
        payload = gui.capture_payload(img_state, img_state.url)
        self.assertFalse(payload["full_page"])  # selector forces full_page=False
        self.assertEqual(payload["selector"], ".hero")
        self.assertEqual(payload["viewport"], {"width": 390, "height": 844, "device_scale_factor": 2})
        self.assertEqual(payload["environment"], {"device": "iphone_14", "color_scheme": "dark"})
        self.assertEqual(payload["engine"], "firefox")
        self.assertEqual(
            payload["cleanup"],
            {
                "consent_mode": "reject",
                "block_ads": True,
                "block_trackers": True,
                "block_chats": True,
                "block_newsletters": True,
            },
        )
        self.assertEqual(payload["image"], {"quality": 80, "transparent_background": True})
        self.assertEqual(payload["wait_for"], {"delay_ms": 2500, "selector": "#ready"})
        self.assertEqual(payload["profile"], "accurate")
        self.assertFalse(payload["stealth"])
        self.assertEqual(payload["diagnostics"], {"bundle": True})
        validate_contract(payload)

        # PDF payload
        pdf_state = gui.GuiState(
            url="https://example.com",
            output="pdf",
            pdf_paper_size="Letter",
            pdf_orientation="landscape",
        )
        pdf_payload = gui.capture_payload(pdf_state, pdf_state.url)
        self.assertEqual(pdf_payload["pdf"], {"paper_size": "Letter", "orientation": "landscape"})
        validate_contract(pdf_payload)

        # Video payload with custom fps and scroll
        vid_state = gui.GuiState(
            url="https://example.com",
            output="webm",
            video_duration_ms=15000,
            video_scroll=True,
            video_fps=30,
        )
        vid_payload = gui.capture_payload(vid_state, vid_state.url)
        self.assertEqual(vid_payload["video"], {"duration_ms": 15000, "scroll": True, "fps": 30})
        validate_contract(vid_payload)

        # Incompatible PDF + Firefox blocked on Enter
        bad_pdf = gui.GuiState(url="https://example.com", output="pdf", engine="firefox")
        next_state, action = gui.apply_key(bad_pdf, "enter")
        self.assertEqual(action, "edit")
        self.assertIn("Chromium", next_state.error)

    def test_looks_like_output_and_extensions_for_all_formats_and_diagnostics(self) -> None:
        samples = [
            ("png", "png", b"\x89PNG\r\n\x1a\n" + b"x"),
            ("jpeg", "jpg", b"\xff\xd8\xff\xe0" + b"x"),
            ("webp", "webp", b"RIFF\x10\x00\x00\x00WEBP" + b"x"),
            ("avif", "avif", b"\x00\x00\x00\x1cftypavif" + b"x"),
            ("pdf", "pdf", b"%PDF-1.7\n"),
            ("gif", "gif", b"GIF89a"),
            ("mp4", "mp4", b"\x00\x00\x00\x18ftypmp42"),
            ("webm", "webm", b"\x1a\x45\xdf\xa3" + b"x"),
            ("html", "html", b"<!doctype html><html><body>hi</body></html>"),
            ("markdown", "md", b"# Hello\n\nWorld\n"),
            ("metadata", "json", b'{"title":"Example"}'),
        ]
        moment = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as tmp:
            for fmt, ext, body in samples:
                with self.subTest(fmt=fmt):
                    self.assertTrue(gui.looks_like_output(fmt, body))
                    self.assertFalse(gui.looks_like_output(fmt, b"\x00\x01\x02\x03"))
                    self.assertEqual(gui.output_extension(fmt), ext)
                    out_path = gui.output_path(Path(tmp), "https://example.com", fmt, moment)
                    self.assertTrue(out_path.name.endswith(f".{ext}"))

            # Diagnostics ZIP bundle
            zip_body = b"PK\x03\x04dummy-zip"
            self.assertTrue(gui.looks_like_output("png", zip_body, diagnostics=True))
            self.assertFalse(gui.looks_like_output("png", b"\x89PNG\r\n\x1a\n", diagnostics=True))
            zip_path = gui.output_path(Path(tmp), "https://example.com", "png", moment, diagnostics=True)
            self.assertTrue(zip_path.name.endswith(".zip"))

    def test_perform_capture_records_telemetry_and_history(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"payload-bytes"

        class HeaderResponse:
            headers = {"X-ViperCapture-Encode-Ms": "275"}
            status = 200

            def __init__(self) -> None:
                self.done = False

            def read(self, _n: int) -> bytes:
                if self.done:
                    return b""
                self.done = True
                return png

            def __enter__(self) -> "HeaderResponse":
                return self

            def __exit__(self, *_a: object) -> bool:
                return False

        with TemporaryDirectory() as tmp:
            state = gui.perform_capture(
                gui.GuiState(url="example.com", busy=True),
                directory=Path(tmp),
                opener=lambda _req, timeout: HeaderResponse(),
            )
            self.assertEqual(state.last_size_bytes, len(png))
            self.assertEqual(state.last_duration_ms, 275)
            self.assertEqual(len(state.captures), 1)
            self.assertEqual(state.captures[0].url, "https://example.com")
            self.assertEqual(state.url_history[0], "https://example.com")


class GuiDrawerAndActionTests(TestCase):
    def test_post_capture_actions_open_copy_and_paste(self) -> None:
        empty = gui.GuiState()
        s_open, a_open = gui.apply_key(empty, "ctrl-o")
        self.assertEqual(a_open, "edit")
        self.assertIn("No saved capture", s_open.error)

        with TemporaryDirectory() as tmp:
            saved_file = Path(tmp) / "shot.png"
            saved_file.write_bytes(b"\x89PNG\r\n\x1a\n")
            state = gui.GuiState(url="https://example.com", saved=str(saved_file))
            cancel = gui.CancelFlag()
            with mock.patch.object(gui, "open_in_viewer", return_value=True) as opener:
                state, _, _, _, quit_flag = gui._handle_key(
                    state, "ctrl-o", None, cancel, {}, Path(tmp), started_server=False
                )
                self.assertFalse(quit_flag)
                opener.assert_called_once_with(str(saved_file))
                self.assertIn("Opened shot.png", state.notice)
            with mock.patch.object(gui, "copy_to_clipboard", return_value=True) as copier:
                state, _, _, _, _ = gui._handle_key(
                    state, "ctrl-y", None, cancel, {}, Path(tmp), started_server=False
                )
                copier.assert_called_once_with(str(saved_file))
                self.assertIn("Copied path", state.notice)
            with mock.patch.object(gui, "read_clipboard", return_value="https://pasted.example\n"):
                fresh = gui.GuiState()
                fresh, _, _, _, _ = gui._handle_key(
                    fresh, "ctrl-v", None, cancel, {}, Path(tmp), started_server=False
                )
                self.assertEqual(fresh.url, "https://pasted.example")

    def test_advanced_options_drawer_navigation_editing_and_gpu_toggle(self) -> None:
        state = gui.GuiState(full_page=True)
        state, action = gui.apply_key(state, "ctrl-g")
        self.assertEqual(state.drawer, "options")
        self.assertEqual(action, "edit")

        # Field 0 is scale -> cycle right -> 2x
        state, _ = gui.apply_key(state, "right")
        self.assertEqual(state.scale, 2)

        # Navigate to selector (index 5) and edit it
        for _ in range(5):
            state, _ = gui.apply_key(state, "down")
        self.assertEqual(gui.OPTIONS_FIELDS[state.drawer_index][0], "selector")
        state, _ = gui.apply_key(state, "enter")
        self.assertEqual(state.editing_field, "selector")
        for ch in "#main":
            state, _ = gui.apply_key(state, ch)
        state, _ = gui.apply_key(state, "enter")
        self.assertEqual(state.editing_field, "")
        self.assertEqual(state.selector, "#main")
        self.assertFalse(state.full_page)

        # Navigate to gpu_mode (last field)
        state = gui._replace(state, drawer_index=len(gui.OPTIONS_FIELDS) - 1)
        state, action = gui.apply_key(state, " ")
        self.assertEqual(action, "toggle_gpu")

        with mock.patch.object(
            gui,
            "set_gpu_mode",
            return_value=({"mode": "auto", "hardware_active": True}, ""),
        ) as set_gpu:
            updated, _, _, _, _ = gui._handle_key(
                state, " ", None, gui.CancelFlag(), {}, Path("."), started_server=False
            )
            set_gpu.assert_called_once()
            self.assertEqual(updated.gpu_mode, "auto")
            self.assertTrue(updated.gpu_hardware)

        # Close drawer with Esc
        closed, action = gui.apply_key(updated, "esc")
        self.assertEqual(action, "edit")
        self.assertEqual(closed.drawer, "")

    def test_presets_drawer_load_apply_save_delete_and_webui_compat(self) -> None:
        web_preset = gui.PresetEntry(
            name="Dark Mobile",
            settings={
                "output": "webp",
                "width": 390,
                "height": 844,
                "density": 2,
                "fullPage": True,
                "quality": 85,
                "colorScheme": "dark",
                "device": "iphone_14",
                "consent": "reject",
                "blockAds": True,
                "videoDuration": 15,
                "waitDelay": 2.5,
                "paperSize": "Letter",
                "orientation": "landscape",
            },
        )
        state = gui.GuiState()
        with mock.patch.object(gui, "fetch_presets", return_value=((web_preset,), "")):
            state, _, _, _, _ = gui._handle_key(
                state, "ctrl-f", None, gui.CancelFlag(), {}, Path("."), started_server=False
            )
        self.assertEqual(state.drawer, "presets")
        self.assertEqual(len(state.presets), 1)

        # Apply with Enter
        applied, _ = gui.apply_key(state, "enter")
        self.assertEqual(applied.drawer, "")
        self.assertEqual(applied.output, "webp")
        self.assertEqual(applied.viewport, (390, 844))
        self.assertEqual(applied.device, "iphone_14")
        self.assertEqual(applied.color_scheme, "dark")
        self.assertEqual(applied.scale, 2)
        self.assertEqual(applied.quality, 85)
        self.assertTrue(applied.cleanup)
        self.assertEqual(applied.video_duration_ms, 15000)
        self.assertEqual(applied.wait_delay_ms, 2500)

        # Round-trip preset settings
        exported = gui.state_to_preset_settings(applied)
        reapplied = gui.apply_preset_settings(gui.GuiState(), exported)
        self.assertEqual(reapplied.output, "webp")
        self.assertEqual(reapplied.viewport, (390, 844))
        self.assertEqual(reapplied.color_scheme, "dark")

        # Save preset ('s') and delete preset ('d')
        state, _ = gui.apply_key(state, "s")
        self.assertEqual(state.editing_field, "preset_name")
        for ch in "My Preset":
            state, _ = gui.apply_key(state, ch)
        new_entry = gui.PresetEntry(name="My Preset", settings=exported)
        with mock.patch.object(gui, "save_preset_remote", return_value=((new_entry, web_preset), "")):
            state, _, _, _, _ = gui._handle_key(
                state, "enter", None, gui.CancelFlag(), {}, Path("."), started_server=False
            )
        self.assertEqual(state.editing_field, "")
        self.assertEqual(len(state.presets), 2)
        with mock.patch.object(gui, "delete_preset_remote", return_value=((web_preset,), "")):
            state, _, _, _, _ = gui._handle_key(
                state, "d", None, gui.CancelFlag(), {}, Path("."), started_server=False
            )
        self.assertEqual(len(state.presets), 1)

    def test_history_drawer_reload_preview_and_help_overlay(self) -> None:
        with TemporaryDirectory() as tmp:
            png_path = Path(tmp) / "shot.png"
            png_bytes = launch.LOGO_PNG.read_bytes()
            png_path.write_bytes(png_bytes)
            rec = gui.CaptureRecord(
                url="https://captured.example",
                output="png",
                path=str(png_path),
                size_bytes=len(png_bytes),
                duration_ms=310,
                timestamp="12:00:00",
                full_page=True,
                color_scheme="dark",
                cleanup=True,
            )
            state = gui.GuiState(captures=(rec,))
            state, _ = gui.apply_key(state, "ctrl-l")
            self.assertEqual(state.drawer, "history")

            # Image-capable terminal renders inline PNG preview
            kitty_frame = gui.render_frame(
                launch.WindowSize(100, 30, 1000, 600),
                state,
                protocol="kitty",
            )
            self.assertIn(b"\x1b_G", kitty_frame)
            self.assertIn(b"Capture History", kitty_frame)

            # Plain terminal renders metadata without image escapes
            plain_frame = gui.render_frame(launch.WindowSize(100, 30), state)
            self.assertNotIn(b"\x1b_G", plain_frame)
            self.assertIn(b"310ms", plain_frame)

            # Reload capture settings with 'r'
            reloaded, _ = gui.apply_key(state, "r")
            self.assertEqual(reloaded.drawer, "")
            self.assertEqual(reloaded.url, "https://captured.example")
            self.assertTrue(reloaded.full_page)
            self.assertEqual(reloaded.color_scheme, "dark")
            self.assertTrue(reloaded.cleanup)

            # Help overlay opens with F1 and Ctrl+/ and closes with Esc
            helped, _ = gui.apply_key(reloaded, "f1")
            self.assertEqual(helped.drawer, "help")
            closed, _ = gui.apply_key(helped, "esc")
            self.assertEqual(closed.drawer, "")
            helped2, _ = gui.apply_key(closed, "ctrl-slash")
            self.assertEqual(helped2.drawer, "help")
            closed2, _ = gui.apply_key(helped2, "f1")
            self.assertEqual(closed2.drawer, "")

    def test_all_drawers_render_within_bounds_across_terminal_sizes(self) -> None:
        import re

        csi_move = re.compile(r"\x1b\[(\d+);(\d+)H")
        rec = gui.CaptureRecord(
            url="https://example.com/very/long/path/for/history",
            output="pdf",
            path="/tmp/vipercapture-example.com-20261124-120000.pdf",
            size_bytes=524_288,
            duration_ms=1450,
            timestamp="12:00:00",
        )
        preset = gui.PresetEntry(name="Mobile Dark PDF", settings={"output": "pdf", "width": 390, "height": 844})
        drawer_states = [
            gui.GuiState(url="https://example.com", color_scheme="dark", engine="firefox", cleanup=True, gpu_known=True, gpu_hardware=True),
            gui.GuiState(drawer="options", drawer_index=5, editing_field="selector", edit_buffer="#content > article"),
            gui.GuiState(drawer="presets", presets=(preset,), editing_field="preset_name", edit_buffer="My Preset"),
            gui.GuiState(drawer="history", captures=(rec,)),
            gui.GuiState(drawer="help", drawer_index=2),
        ]
        sizes = (
            launch.WindowSize(14, 6),
            launch.WindowSize(20, 8),
            launch.WindowSize(40, 14),
            launch.WindowSize(80, 24),
            launch.WindowSize(120, 40),
        )
        for size in sizes:
            for state in drawer_states:
                with self.subTest(size=(size.cols, size.rows), drawer=state.drawer or "main"):
                    frame = gui.render_frame(size, state).decode("utf-8", errors="replace")
                    parts = csi_move.split(frame)
                    for idx in range(1, len(parts), 3):
                        row, col = int(parts[idx]), int(parts[idx + 1])
                        vis = gui.visible(parts[idx + 2])
                        self.assertLessEqual(row, size.rows)
                        self.assertLessEqual(
                            col + gui._display_width(vis) - 1,
                            size.cols,
                            f"Overflow at {size.cols}x{size.rows} ({state.drawer}): {vis!r}",
                        )


class GuiCouncilHardeningTests(TestCase):
    def test_remote_capture_scoping_never_leaks_admin_token_or_http_key(self) -> None:
        env = {
            "VIPERCAPTURE_ADMIN_TOKEN": "admin-secret",
            "VIPERCAPTURE_API_KEY": "project-key",
        }
        # Default local endpoint can use admin when only admin is set
        self.assertEqual(
            gui.capture_credential(
                started_server=False,
                environ={"VIPERCAPTURE_ADMIN_TOKEN": "admin-secret"},
                endpoint="http://127.0.0.1:8000/v1/render",
            ),
            "admin-secret",
        )
        # Remote plain HTTP sends neither admin nor project key
        self.assertEqual(
            gui.capture_credential(
                started_server=False,
                environ=env,
                endpoint="http://remote.example/v1/render",
            ),
            "",
        )
        # Remote HTTPS sends project key, never admin token
        self.assertEqual(
            gui.capture_credential(
                started_server=False,
                environ=env,
                endpoint="https://remote.example/v1/render",
            ),
            "project-key",
        )
        self.assertEqual(
            gui.capture_credential(
                started_server=False,
                environ={"VIPERCAPTURE_ADMIN_TOKEN": "admin-secret"},
                endpoint="https://remote.example/v1/render",
            ),
            "",
        )

    def test_terminal_control_sequences_stripped_from_errors_and_presets(self) -> None:
        malicious = b'{"detail":"bad \\u001b]52;c;YWJj\\u0007 \\u001b[31mboom\\u001b[0m\\u0007"}'
        cleaned = gui.error_text(500, malicious)
        self.assertNotIn("\x1b", cleaned)
        self.assertNotIn("\x07", cleaned)
        self.assertIn("bad", cleaned)

        presets = gui._parse_presets_body(
            {"presets": [{"name": "Dark\x1b[2J\x1b]0;pwn\x07Mode", "settings": {"output": "png"}}]}
        )
        self.assertEqual(len(presets), 1)
        # Exact server identifier is preserved for delete/lookup; rendered frame strips escapes.
        self.assertEqual(presets[0].name, "Dark\x1b[2J\x1b]0;pwn\x07Mode")
        preset_frame = gui.render_frame(
            launch.WindowSize(80, 24),
            gui.GuiState(drawer="presets", presets=presets),
        ).decode("utf-8")
        self.assertNotIn("\x1b]0;pwn", preset_frame)
        self.assertNotIn("\x1b[2J", preset_frame[4:])
        self.assertNotIn("\x07", preset_frame)
        self.assertIn("DarkMode", preset_frame)

        state = gui.GuiState(error="err\x1b]52;c;ZGF0YQ==\x07msg")
        frame = gui.render_frame(launch.WindowSize(80, 24), state).decode("utf-8")
        self.assertNotIn("\x1b]52", frame)
        self.assertNotIn("\x07", frame)

    def test_oversized_bracketed_paste_and_split_delimiters_never_emit_enter(self) -> None:
        pending = bytearray()
        payload = b"\x1b[200~" + (b"https://example.com/" + b"a" * 17_000 + b"\n") + b"\x1b[201~"
        emitted: list[str] = []
        # Feed in 256-byte chunks up to the final 10 bytes, then byte-by-byte across the closing delimiter
        split_at = len(payload) - 10
        for idx in range(0, split_at, 256):
            emitted.extend(gui.keys_from_bytes(pending, payload[idx : min(idx + 256, split_at)]))
        for byte in payload[split_at:]:
            emitted.extend(gui.keys_from_bytes(pending, bytes([byte])))
        self.assertNotIn("enter", emitted)
        self.assertEqual(len(emitted), 1)
        self.assertTrue(emitted[0].startswith("paste:https://example.com/"))
        self.assertEqual(len(pending), 0)

    def test_app_config_filters_available_output_formats(self) -> None:
        state = gui.GuiState(output="mp4")
        updated = gui.apply_app_config(state, {"output_formats": ["png", "webp", "pdf"]})
        self.assertEqual(updated.available_formats, ("png", "webp", "pdf"))
        self.assertEqual(updated.output, "png")
        cycled, _ = gui.apply_key(updated, "tab")
        self.assertEqual(cycled.output, "webp")
        cycled, _ = gui.apply_key(cycled, "tab")
        self.assertEqual(cycled.output, "pdf")
        cycled, _ = gui.apply_key(cycled, "tab")
        self.assertEqual(cycled.output, "png")
        self.assertNotIn("mp4", updated.available_formats)

    def test_windows_delayed_paste_chunk_stays_in_paste_mode(self) -> None:
        class FakeMsvcrt:
            def __init__(self, chunks: list[str]) -> None:
                self._buf = list(chunks)

            def kbhit(self) -> bool:
                return bool(self._buf)

            def getwch(self) -> str:
                return self._buf.pop(0)

        pending = bytearray()
        first = FakeMsvcrt(list("[200~https://delayed."))
        self.assertEqual(gui._drain_windows_escape(first, pending), [])
        self.assertTrue(pending.startswith(gui._PASTE_START))
        second = FakeMsvcrt(list("example\x1b[201~"))
        self.assertEqual(gui._drain_windows_escape(second, pending), ["paste:https://delayed.example"])
        self.assertEqual(len(pending), 0)

    def test_mobile_preset_sends_explicit_1x_dpr_and_preset_preserves_custom_values(self) -> None:
        mobile = gui.GuiState(url="https://example.com", viewport=(390, 844), device="iphone_14", scale=1)
        payload = gui.capture_payload(mobile, mobile.url)
        self.assertEqual(payload["viewport"]["device_scale_factor"], 1)  # type: ignore[index]

        # Web UI preset with float density, 24fps, accept consent, trackers off, and unsupported headers
        preset_settings = {
            "output": "mp4",
            "width": 1280,
            "height": 720,
            "density": 1.5,
            "videoFps": 24,
            "consent": "accept",
            "blockAds": True,
            "blockTrackers": False,
            "blockChats": False,
            "blockNewsletters": False,
            "headers": '{"X-Test":"1"}',
            "customCss": "body{color:red}",
        }
        updated, ignored = gui.apply_preset_settings_with_warnings(gui.GuiState(), preset_settings)
        self.assertEqual(updated.scale, 1.5)
        self.assertEqual(updated.video_fps, 24)
        self.assertIn("headers", ignored)
        self.assertIn("customCss", ignored)
        doc = gui.capture_payload(updated, "https://example.com")
        self.assertEqual(doc["viewport"]["device_scale_factor"], 1.5)  # type: ignore[index]
        self.assertEqual(doc["video"]["fps"], 24)  # type: ignore[index]
        self.assertEqual(
            doc["cleanup"],
            {
                "consent_mode": "accept",
                "block_ads": True,
                "block_trackers": False,
                "block_chats": False,
                "block_newsletters": False,
            },
        )

    def test_text_output_validation_allows_empty_markdown_and_rejects_3xx(self) -> None:
        class RedirectResponse:
            status = 302
            headers = {"Content-Type": "text/html"}

            def __init__(self) -> None:
                self._done = False

            def read(self, _n: int) -> bytes:
                if self._done:
                    return b""
                self._done = True
                return b"<html>redirect</html>"

            def __enter__(self) -> "RedirectResponse":
                return self

            def __exit__(self, *_a: object) -> bool:
                return False

        class EmptyMarkdownResponse:
            status = 200
            headers = {"Content-Type": "text/markdown; charset=utf-8"}

            def read(self, _n: int) -> bytes:
                return b""

            def __enter__(self) -> "EmptyMarkdownResponse":
                return self

            def __exit__(self, *_a: object) -> bool:
                return False

        with TemporaryDirectory() as tmp:
            res_redirect = gui.perform_capture(
                gui.GuiState(url="https://example.com", output="html", busy=True),
                directory=Path(tmp),
                opener=lambda _req, timeout: RedirectResponse(),
            )
            self.assertTrue(res_redirect.error)
            self.assertEqual(res_redirect.saved, "")

            res_empty_md = gui.perform_capture(
                gui.GuiState(url="https://example.com", output="markdown", busy=True),
                directory=Path(tmp),
                opener=lambda _req, timeout: EmptyMarkdownResponse(),
            )
            self.assertEqual(res_empty_md.error, "")
            self.assertTrue(res_empty_md.saved.endswith(".md"))

    def test_editing_url_keeps_ctrl_o_and_ctrl_y_working(self) -> None:
        with TemporaryDirectory() as tmp:
            saved_file = Path(tmp) / "shot.png"
            saved_file.write_bytes(b"\x89PNG\r\n\x1a\n")
            rec = gui.CaptureRecord(
                url="https://example.com",
                output="png",
                path=str(saved_file),
                size_bytes=8,
                duration_ms=50,
                timestamp="12:00:00",
            )
            state = gui.GuiState(url="https://example.com", saved=str(saved_file), captures=(rec,))
            edited, _ = gui.apply_key(state, "x")
            self.assertEqual(edited.saved, "")
            _, act_open = gui.apply_key(edited, "ctrl-o")
            _, act_copy = gui.apply_key(edited, "ctrl-y")
            self.assertEqual(act_open, "open_last")
            self.assertEqual(act_copy, "copy_last")

    def test_sixel_multicolor_gradient_and_oversized_png_budget(self) -> None:
        import struct
        import zlib

        def make_png(w: int, h: int, raw_scanlines: bytes) -> bytes:
            def chunk(kind: bytes, data: bytes) -> bytes:
                return (
                    struct.pack(">I", len(data))
                    + kind
                    + data
                    + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
                )

            ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
            return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw_scanlines)) + chunk(b"IEND", b"")

        # 64x64 multicolor RGB gradient (>256 unique quantized colors at 5 bits)
        rows = bytearray()
        for y in range(64):
            rows.append(0)
            for x in range(64):
                rows.extend(((x * 4) & 255, (y * 4) & 255, ((x + y) * 2) & 255))
        grad_png = make_png(64, 64, bytes(rows))
        rec = gui.CaptureRecord(
            url="https://example.com",
            output="png",
            path="grad.png",
            size_bytes=len(grad_png),
            duration_ms=100,
            timestamp="12:00:00",
        )
        state = gui.GuiState(drawer="history", captures=(rec,))
        sixel_frame = gui.render_frame(
            launch.WindowSize(100, 30, 800, 480),
            state,
            protocol="sixel",
            preview_png=grad_png,
        )
        self.assertIn(b"\x1bPq", sixel_frame)

        # Oversized PNG header (10000x10000 = 100M pixels) must skip Sixel decompression
        fake_huge_header = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">IIBBBBB", 10_000, 10_000, 8, 2, 0, 0, 0)
        fallback_frame = gui.render_frame(
            launch.WindowSize(100, 30, 800, 480),
            state,
            protocol="sixel",
            preview_png=fake_huge_header,
        )
        self.assertNotIn(b"\x1bPq", fallback_frame)
        self.assertIn(b"Capture History", fallback_frame)

        # PNG zlib decompression bomb (small 10x10 header, huge decompressed IDAT) must raise & skip Sixel
        bomb_png = make_png(10, 10, b"\x00" * 200_000)
        with self.assertRaises(ValueError):
            launch.png_rgba(bomb_png)
        bomb_frame = gui.render_frame(
            launch.WindowSize(100, 30, 800, 480),
            state,
            protocol="sixel",
            preview_png=bomb_png,
        )
        self.assertNotIn(b"\x1bPq", bomb_frame)

        # Truncated zlib stream (full raster present, 4-byte Adler-32 checksum stripped) must be rejected
        small_rows = b"\x00" + b"\x11\x22\x33" * 4
        compressed_trunc = zlib.compress(small_rows)[:-4]
        def _chunk(kind: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        trunc_png = (
            b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 1, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", compressed_trunc)
            + _chunk(b"IEND", b"")
        )
        with self.assertRaises(ValueError):
            launch.png_rgba(trunc_png)

    def test_preset_exact_name_preserved_on_delete_and_summary_sanitized(self) -> None:
        presets = gui._parse_presets_body(
            {
                "presets": [
                    {
                        "name": "Web  PDF",
                        "settings": {
                            "output": "\x1b]52;c;ZGF0YQ==\x07png",
                            "colorScheme": "\x1b[31mdark\x07",
                        },
                    },
                    {"name": "Web PDF", "settings": {"output": "pdf"}},
                ]
            }
        )
        self.assertEqual([p.name for p in presets], ["Web  PDF", "Web PDF"])
        summary = gui._preset_summary(presets[0].settings)
        self.assertNotIn("\x1b", summary)
        self.assertNotIn("\x07", summary)
        state = gui.GuiState(drawer="presets", presets=presets, drawer_index=0)
        frame = gui.render_frame(launch.WindowSize(80, 24), state).decode("utf-8")
        self.assertNotIn("\x1b]52", frame)
        self.assertNotIn("\x07", frame)

        with mock.patch.object(gui, "delete_preset_remote", return_value=((presets[1],), "")) as del_mock:
            gui._handle_key(state, "d", None, gui.CancelFlag(), {}, Path("."), started_server=False)
        del_mock.assert_called_once()
        self.assertEqual(del_mock.call_args.args[1], "Web  PDF")

    def test_preset_checks_available_formats_and_reports_extra_ignored_fields(self) -> None:
        base = gui.GuiState(output="png", available_formats=("png", "webp"))
        updated, ignored = gui.apply_preset_settings_with_warnings(
            base,
            {
                "output": "mp4",
                "waitTimeout": 30,
                "pdfMargin": 12,
                "colorScheme": "no-preference",
            },
        )
        self.assertEqual(updated.output, "png")
        self.assertIn("output(mp4)", ignored)
        self.assertIn("waitTimeout", ignored)
        self.assertIn("pdfMargin", ignored)
        self.assertIn("colorScheme", ignored)

        # Startup --preset must run after /app-config restricts available_formats
        mp4_preset = gui.PresetEntry(name="Video", settings={"output": "mp4", "width": 1280, "height": 720})
        captured_initial: list[gui.GuiState] = []

        def fake_run_keys(
            initial: gui.GuiState,
            *_args: object,
            config_loaded: bool = False,
            **_kwargs: object,
        ) -> gui.GuiState:
            self.assertTrue(config_loaded)
            captured_initial.append(initial)
            return initial

        with (
            mock.patch.object(gui, "fetch_app_config", return_value={"output_formats": ["png", "webp"]}),
            mock.patch.object(gui, "fetch_presets", return_value=((mp4_preset,), "")),
            mock.patch.object(gui, "load_url_history", return_value=()),
            mock.patch.object(gui, "_run_keys", side_effect=fake_run_keys),
            mock.patch.object(gui, "_enter"),
            mock.patch.object(gui, "_leave"),
            mock.patch.object(gui, "_enable_windows_vt", return_value=None),
            mock.patch.object(gui, "_restore_windows_vt"),
            mock.patch.object(launch, "choose_graphics_protocol", return_value="none"),
            mock.patch.object(launch, "port_open", return_value=True),
            mock.patch.object(launch, "ensure_deps"),
            mock.patch.object(launch, "ensure_playwright"),
            mock.patch.object(gui.sys, "stdin", mock.Mock(isatty=lambda: True)),
            mock.patch.object(gui.sys, "stdout", mock.Mock(isatty=lambda: True)),
        ):
            gui.run_capture_gui((1920, 1080), preset="Video")
        self.assertEqual(len(captured_initial), 1)
        self.assertEqual(captured_initial[0].output, "png")
        self.assertIn("output(mp4)", captured_initial[0].notice)

    def test_windows_split_opening_paste_marker_stays_in_paste_mode(self) -> None:
        class FakeMsvcrt:
            def __init__(self, chunks: list[str]) -> None:
                self._buf = list(chunks)

            def kbhit(self) -> bool:
                return bool(self._buf)

            def getwch(self) -> str:
                return self._buf.pop(0)

        pending = bytearray()
        # Opening \x1b[200~ split across Windows reads: first read gets \x1b then "[20"
        self.assertEqual(gui._drain_windows_escape(FakeMsvcrt(list("[20")), pending), [])
        self.assertEqual(bytes(pending), b"\x1b[20")
        self.assertEqual(gui._drain_windows_escape(FakeMsvcrt(list("0~https://split.\n")), pending), [])
        self.assertTrue(pending.startswith(gui._PASTE_START))
        emitted = gui._drain_windows_escape(FakeMsvcrt(list("example\x1b[201~")), pending)
        self.assertEqual(emitted, ["paste:https://split.\nexample"])
        self.assertEqual(len(pending), 0)

    def test_windows_delayed_paste_immediately_after_escape_and_standalone_escape(self) -> None:
        import time as _time

        class DelayedAfterEsc:
            def __init__(self, delay_s: float, tail: str) -> None:
                self._ready_at = _time.monotonic() + delay_s
                self._buf = list(tail)

            def kbhit(self) -> bool:
                return bool(self._buf) and _time.monotonic() >= self._ready_at

            def getwch(self) -> str:
                return self._buf.pop(0)

        class EmptyMsvcrt:
            def kbhit(self) -> bool:
                return False

            def getwch(self) -> str:
                raise AssertionError("should not read")

        # Chunk arriving 45ms after initial Escape (>25ms) must enter paste mode, not emit esc
        pending = bytearray()
        emitted = gui._drain_windows_escape(
            DelayedAfterEsc(0.045, "[200~https://after-esc.example\x1b[201~"),
            pending,
            paste_grace=0.15,
        )
        self.assertEqual(emitted, ["paste:https://after-esc.example"])
        self.assertEqual(len(pending), 0)

        # Standalone Escape with no following bytes still flushes as esc after grace timeout
        bare_pending = bytearray()
        self.assertEqual(
            gui._drain_windows_escape(EmptyMsvcrt(), bare_pending, paste_grace=0.02),
            ["esc"],
        )
        self.assertEqual(len(bare_pending), 0)

    def test_preset_huge_numeric_fields_and_startup_config_retry_and_url_sanitization(self) -> None:
        # Huge JSON ints must not raise OverflowError in preset warning detection
        huge = 10**400
        _st, ignored = gui.apply_preset_settings_with_warnings(
            gui.GuiState(),
            {"waitTimeout": huge, "pdfMargin": huge, "videoBitrate": huge},
        )
        self.assertIn("waitTimeout", ignored)
        self.assertIn("pdfMargin", ignored)
        self.assertIn("videoBitrate", ignored)

        # Transparent full-page GIF & WebM presets emit video.transparent_background
        for fmt in ("gif", "webm"):
            st = gui.apply_preset_settings(gui.GuiState(), {"output": fmt, "fullPage": True, "transparent": True})
            doc = gui.capture_payload(st, "https://example.com")
            self.assertTrue(doc["video"]["transparent_background"])  # type: ignore[index]
        st_vp = gui.apply_preset_settings(gui.GuiState(), {"output": "webm", "fullPage": False, "transparent": True})
        self.assertNotIn("transparent_background", gui.capture_payload(st_vp, "https://example.com")["video"])  # type: ignore[operator]

        # Startup config retry finishes before --preset is applied; startup --url is sanitized
        mp4_preset = gui.PresetEntry(name="Video", settings={"output": "mp4", "width": 1280, "height": 720})
        captured: list[gui.GuiState] = []

        def fake_run_keys(
            initial: gui.GuiState,
            *_args: object,
            config_loaded: bool = False,
            **_kwargs: object,
        ) -> gui.GuiState:
            self.assertTrue(config_loaded)
            captured.append(initial)
            return initial

        with (
            mock.patch.object(
                gui,
                "fetch_app_config",
                side_effect=[None, {"output_formats": ["png", "webp"]}],
            ) as cfg_mock,
            mock.patch.object(gui, "fetch_presets", return_value=((mp4_preset,), "")),
            mock.patch.object(gui, "load_url_history", return_value=()),
            mock.patch.object(gui, "_run_keys", side_effect=fake_run_keys),
            mock.patch.object(gui, "_enter"),
            mock.patch.object(gui, "_leave"),
            mock.patch.object(gui, "_enable_windows_vt", return_value=None),
            mock.patch.object(gui, "_restore_windows_vt"),
            mock.patch.object(launch, "choose_graphics_protocol", return_value="none"),
            mock.patch.object(launch, "port_open", return_value=True),
            mock.patch.object(launch, "ensure_deps"),
            mock.patch.object(launch, "ensure_playwright"),
            mock.patch.object(gui.sys, "stdin", mock.Mock(isatty=lambda: True)),
            mock.patch.object(gui.sys, "stdout", mock.Mock(isatty=lambda: True)),
        ):
            gui.run_capture_gui(
                (1920, 1080),
                preset="Video",
                initial_url="https://example.com/\x1b]52;c;ZGF0YQ==\x07ok",
            )
        self.assertEqual(cfg_mock.call_count, 2)
        self.assertEqual(captured[0].output, "png")
        self.assertIn("output(mp4)", captured[0].notice)
        self.assertEqual(captured[0].url, "https://example.com/ok")
        self.assertEqual(captured[0].cursor, len("https://example.com/ok"))

        # History recall sanitizes stored URLs and computes cursor from cleaned length
        dirty_state = gui.GuiState(url_history=("https://hist.example/\x1b[31mred\x07",))
        recalled, _ = gui.apply_key(dirty_state, "up")
        self.assertEqual(recalled.url, "https://hist.example/red")
        self.assertEqual(recalled.cursor, len("https://hist.example/red"))
        frame = gui.render_frame(launch.WindowSize(80, 24), recalled).decode("utf-8")
        self.assertNotIn("\x1b[31m", frame)
        self.assertNotIn("\x07", frame)

    def test_remote_capture_uses_configured_proxy_and_local_bypasses_proxy(self) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading as _threading

        seen_requests: list[tuple[str, str]] = []

        class ProxyHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                seen_requests.append((self.path, self.headers.get("Proxy-Authorization", "")))
                body = b"\x89PNG\r\n\x1a\nproxied"
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        proxy_srv = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
        t = _threading.Thread(target=proxy_srv.serve_forever, daemon=True)
        t.start()
        try:
            proxy_port = proxy_srv.server_address[1]
            with (
                mock.patch.object(
                    gui.urllib.request,
                    "getproxies",
                    return_value={"http": f"http://alice:pw@127.0.0.1:{proxy_port}"},
                ),
                mock.patch.object(gui.urllib.request, "proxy_bypass_environment", return_value=False),
                mock.patch.object(gui.urllib.request, "proxy_bypass", return_value=False),
            ):
                # Local loopback must bypass the proxy
                self.assertIsNone(gui._proxy_for_url("http://127.0.0.1:8000/v1/render", "http", "127.0.0.1"))
                # Remote HTTP capture routes through the proxy with Proxy-Authorization
                req = gui.urllib.request.Request(
                    "http://remote.example/v1/render",
                    data=b'{"url":"https://example.com","output":"png"}',
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with gui._open_capture(req, timeout=5.0, cancel=None) as resp:
                    self.assertEqual(resp.status, 200)
                    self.assertEqual(resp.read(64), b"\x89PNG\r\n\x1a\nproxied")
            self.assertEqual(len(seen_requests), 1)
            self.assertEqual(seen_requests[0][0], "http://remote.example/v1/render")
            self.assertTrue(seen_requests[0][1].startswith("Basic "))
        finally:
            proxy_srv.shutdown()
            proxy_srv.server_close()

    def test_delete_preset_with_slash_in_name_via_http_endpoint(self) -> None:
        try:
            from fastapi.testclient import TestClient
            from vipercapture import main as vc_main
        except ImportError:
            self.skipTest("fastapi/vipercapture server dependencies not installed")

        with TemporaryDirectory() as tmp:
            presets_dir = Path(tmp)
            presets_file = presets_dir / "presets.json"
            with (
                mock.patch.object(vc_main, "PRESETS_DIRECTORY", presets_dir),
                mock.patch.object(vc_main, "PRESETS_FILE", presets_file),
            ):
                client = TestClient(
                    vc_main.app,
                    base_url="http://127.0.0.1:8000",
                    client=("127.0.0.1", 50000),
                )
                created = client.post(
                    "/local/presets",
                    json={"name": "Desktop / PDF", "settings": {"output": "pdf"}},
                )
                self.assertEqual(created.status_code, 201)
                self.assertEqual([p["name"] for p in created.json()["presets"]], ["Desktop / PDF"])

                encoded = gui.urllib.parse.quote("Desktop / PDF", safe="")
                deleted = client.delete(f"/local/presets/{encoded}")
                self.assertEqual(deleted.status_code, 200)
                self.assertEqual(deleted.json()["presets"], [])

    def test_round4_proxy_tls_noproxy_cancel_huge_preset_and_cursor_alignment(self) -> None:
        import socket as _socket
        import threading as _threading
        import time as _time

        # 1. Port-specific NO_PROXY and 127.0.0.2 loopback bypass
        proxies = {
            "https": "http://127.0.0.1:8888",
            "no": "capture.example:8443,[::ffff:192.0.2.1]:9443",
        }
        with (
            mock.patch.object(gui.urllib.request, "getproxies", return_value=proxies),
            mock.patch.object(gui.urllib.request, "proxy_bypass", return_value=False),
        ):
            self.assertIsNone(
                gui._proxy_for_url("https://capture.example:8443/v1/render", "https", "capture.example", 8443)
            )
            self.assertEqual(
                gui._proxy_for_url("https://capture.example:443/v1/render", "https", "capture.example", 443),
                "http://127.0.0.1:8888",
            )
            self.assertIsNone(
                gui._proxy_for_url("http://127.0.0.2:8000/v1/render", "http", "127.0.0.2", 8000)
            )

        # 2. HTTPS proxy for HTTPS target starts TLS handshake BEFORE sending CONNECT/credentials
        first_bytes: list[bytes] = []
        srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        tls_proxy_port = srv.getsockname()[1]

        def _accept_once() -> None:
            conn, _addr = srv.accept()
            try:
                conn.settimeout(2.0)
                first_bytes.append(conn.recv(64))
            finally:
                conn.close()

        t_tls = _threading.Thread(target=_accept_once, daemon=True)
        t_tls.start()
        try:
            with (
                mock.patch.object(
                    gui.urllib.request,
                    "getproxies",
                    return_value={"https": f"https://alice:secret@127.0.0.1:{tls_proxy_port}"},
                ),
                mock.patch.object(gui.urllib.request, "proxy_bypass_environment", return_value=False),
                mock.patch.object(gui.urllib.request, "proxy_bypass", return_value=False),
            ):
                req = gui.urllib.request.Request("https://remote.example/v1/render", data=b"{}", method="POST")
                with self.assertRaises(Exception):
                    gui._open_capture(req, timeout=2.0, cancel=None)
        finally:
            t_tls.join(timeout=2.0)
            srv.close()
        self.assertTrue(first_bytes and first_bytes[0].startswith(b"\x16\x03"))
        self.assertNotIn(b"CONNECT", first_bytes[0])
        self.assertNotIn(b"secret", first_bytes[0])

        # 3. CancelFlag aborts a stalled proxy CONNECT immediately
        hold_evt = _threading.Event()
        connected_evt = _threading.Event()
        stall_srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        stall_srv.bind(("127.0.0.1", 0))
        stall_srv.listen(1)
        stall_port = stall_srv.getsockname()[1]

        def _stall_proxy() -> None:
            conn, _addr = stall_srv.accept()
            try:
                conn.settimeout(5.0)
                conn.recv(512)
                connected_evt.set()
                hold_evt.wait(timeout=5.0)
            finally:
                conn.close()

        t_stall = _threading.Thread(target=_stall_proxy, daemon=True)
        t_stall.start()
        cancel = gui.CancelFlag()
        outcome: list[object] = []

        def _worker() -> None:
            with (
                mock.patch.object(
                    gui.urllib.request,
                    "getproxies",
                    return_value={"https": f"http://127.0.0.1:{stall_port}"},
                ),
                mock.patch.object(gui.urllib.request, "proxy_bypass_environment", return_value=False),
                mock.patch.object(gui.urllib.request, "proxy_bypass", return_value=False),
            ):
                req = gui.urllib.request.Request("https://remote.example/v1/render", data=b"{}", method="POST")
                try:
                    gui._open_capture(req, timeout=30.0, cancel=cancel)
                except Exception as exc:
                    outcome.append(exc)

        w = _threading.Thread(target=_worker, daemon=True)
        w.start()
        self.assertTrue(connected_evt.wait(timeout=3.0))
        started = _time.monotonic()
        cancel.abort()
        w.join(timeout=3.0)
        elapsed = _time.monotonic() - started
        hold_evt.set()
        t_stall.join(timeout=2.0)
        stall_srv.close()
        self.assertFalse(w.is_alive())
        self.assertLess(elapsed, 2.0)
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], gui._Cancelled)

        # 4. Huge JSON numbers in density/videoDuration/waitDelay do not raise OverflowError
        huge = 10**400
        st_huge, _ignored = gui.apply_preset_settings_with_warnings(
            gui.GuiState(),
            {
                "width": huge,
                "height": huge,
                "density": huge,
                "quality": huge,
                "videoDuration": huge,
                "videoFps": huge,
                "waitDelay": huge,
            },
        )
        self.assertEqual(st_huge.scale, 1)
        self.assertEqual(st_huge.video_duration_ms, 4000)

        # 5. Down-arrow recall sanitizes URL and _render_edit_field_ansi aligns cursor past stripped escapes
        st_hist = gui.GuiState(
            url_history=("https://first.example/\x1b[31mone\x07", "https://second.example/\x1b[32mtwo\x07"),
            history_index=1,
            url="https://second.example/two",
        )
        down_st, _ = gui.apply_key(st_hist, "down")
        self.assertEqual(down_st.url, "https://first.example/one")
        self.assertEqual(down_st.cursor, len("https://first.example/one"))

        rendered = gui._render_edit_field_ansi("\x1b[31mabc", 6, 10)
        self.assertIn("a\x1b[7mb\x1b[0mc", rendered)

    def test_round5_tls_in_tls_proxy_cancel_handshake_idna_noproxy_and_path_scoping(self) -> None:
        import socket as _socket
        import ssl as _ssl
        import threading as _threading
        import time as _time

        # 1. Admin-token fallback rejected for path-prefixed local service on port 8000
        self.assertEqual(
            gui.capture_credential(
                started_server=False,
                environ={"VIPERCAPTURE_ADMIN_TOKEN": "admin-secret"},
                endpoint="http://127.0.0.1:8000/other-app/v1/render",
            ),
            "",
        )

        # 2. Implicit default port matches port-specific NO_PROXY=capture.example:443
        proxies = {"https": "http://127.0.0.1:8888", "no": "capture.example:443"}
        with (
            mock.patch.object(gui.urllib.request, "getproxies", return_value=proxies),
            mock.patch.object(gui.urllib.request, "proxy_bypass", return_value=False),
        ):
            self.assertIsNone(
                gui._proxy_for_url("https://capture.example/v1/render", "https", "capture.example", 443)
            )

        # 3. IDNA hostname encodes to ASCII punycode in CONNECT without UnicodeEncodeError
        seen_connect: list[bytes] = []
        idna_srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        idna_srv.bind(("127.0.0.1", 0))
        idna_srv.listen(1)
        idna_port = idna_srv.getsockname()[1]

        def _accept_idna() -> None:
            conn, _ = idna_srv.accept()
            try:
                conn.settimeout(2.0)
                seen_connect.append(conn.recv(512))
                conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            finally:
                conn.close()

        t_idna = _threading.Thread(target=_accept_idna, daemon=True)
        t_idna.start()
        try:
            with (
                mock.patch.object(
                    gui.urllib.request,
                    "getproxies",
                    return_value={"https": f"http://127.0.0.1:{idna_port}"},
                ),
                mock.patch.object(gui.urllib.request, "proxy_bypass_environment", return_value=False),
                mock.patch.object(gui.urllib.request, "proxy_bypass", return_value=False),
            ):
                req = gui.urllib.request.Request("https://münchen.example/v1/render", data=b"{}", method="POST")
                with self.assertRaises(gui.http.client.HTTPException):
                    gui._open_capture(req, timeout=2.0, cancel=None)
        finally:
            t_idna.join(timeout=2.0)
            idna_srv.close()
        self.assertTrue(seen_connect)
        self.assertIn(b"CONNECT xn--mnchen-3ya.example:443 HTTP/1.1", seen_connect[0])

        # 4. Escape cancels a stalled TLS handshake immediately (not waiting for timeout)
        hold_evt = _threading.Event()
        hello_seen = _threading.Event()
        stall_srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        stall_srv.bind(("127.0.0.1", 0))
        stall_srv.listen(1)
        stall_port = stall_srv.getsockname()[1]

        def _stall_tls() -> None:
            conn, _ = stall_srv.accept()
            try:
                conn.settimeout(5.0)
                conn.recv(512)
                hello_seen.set()
                hold_evt.wait(timeout=5.0)
            finally:
                conn.close()

        t_stall = _threading.Thread(target=_stall_tls, daemon=True)
        t_stall.start()
        cancel = gui.CancelFlag()
        outcome: list[object] = []

        def _worker() -> None:
            with (
                mock.patch.object(
                    gui.urllib.request,
                    "getproxies",
                    return_value={"https": f"https://127.0.0.1:{stall_port}"},
                ),
                mock.patch.object(gui.urllib.request, "proxy_bypass_environment", return_value=False),
                mock.patch.object(gui.urllib.request, "proxy_bypass", return_value=False),
            ):
                req = gui.urllib.request.Request("https://remote.example/v1/render", data=b"{}", method="POST")
                try:
                    gui._open_capture(req, timeout=30.0, cancel=cancel)
                except Exception as exc:
                    outcome.append(exc)

        w = _threading.Thread(target=_worker, daemon=True)
        w.start()
        self.assertTrue(hello_seen.wait(timeout=3.0))
        t0 = _time.monotonic()
        cancel.abort()
        w.join(timeout=3.0)
        elapsed = _time.monotonic() - t0
        hold_evt.set()
        t_stall.join(timeout=2.0)
        stall_srv.close()
        self.assertFalse(w.is_alive())
        self.assertLess(elapsed, 2.0)
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], gui._Cancelled)

        # 5. End-to-end verified HTTPS-in-HTTPS proxy capture returns full PNG response body
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.x509.oid import NameOID
            import datetime as _dt
            import ipaddress as _ip
        except ImportError:
            return

        with TemporaryDirectory() as tmp:
            def _make_cert(cn: str, san_dns: list[str], san_ips: list[str], prefix: str) -> tuple[Path, Path]:
                key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
                now = _dt.datetime.now(_dt.timezone.utc)
                names = [x509.DNSName(d) for d in san_dns] + [x509.IPAddress(_ip.ip_address(ip)) for ip in san_ips]
                cert = (
                    x509.CertificateBuilder()
                    .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
                    .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
                    .public_key(key.public_key())
                    .serial_number(x509.random_serial_number())
                    .not_valid_before(now - _dt.timedelta(minutes=5))
                    .not_valid_after(now + _dt.timedelta(hours=1))
                    .add_extension(x509.SubjectAlternativeName(names), critical=False)
                    .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                    .sign(key, hashes.SHA256())
                )
                cert_p = Path(tmp) / f"{prefix}.crt"
                key_p = Path(tmp) / f"{prefix}.key"
                cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
                key_p.write_bytes(
                    key.private_bytes(
                        serialization.Encoding.PEM,
                        serialization.PrivateFormat.TraditionalOpenSSL,
                        serialization.NoEncryption(),
                    )
                )
                return cert_p, key_p

            proxy_cert, proxy_key = _make_cert("127.0.0.1", ["localhost"], ["127.0.0.1"], "proxy")
            dest_cert, dest_key = _make_cert("capture.example", ["capture.example"], [], "dest")
            ca_bundle = Path(tmp) / "ca.pem"
            ca_bundle.write_bytes(proxy_cert.read_bytes() + b"\n" + dest_cert.read_bytes())

            dest_srv_ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
            dest_srv_ctx.load_cert_chain(str(dest_cert), str(dest_key))
            proxy_srv_ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
            proxy_srv_ctx.load_cert_chain(str(proxy_cert), str(proxy_key))

            dest_sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            dest_sock.bind(("127.0.0.1", 0))
            dest_sock.listen(1)
            dest_port = dest_sock.getsockname()[1]

            proxy_sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            proxy_sock.bind(("127.0.0.1", 0))
            proxy_sock.listen(1)
            proxy_port = proxy_sock.getsockname()[1]

            png_body = b"\x89PNG\r\n\x1a\ntls-in-tls-ok"

            def _run_dest() -> None:
                raw, _ = dest_sock.accept()
                tls = dest_srv_ctx.wrap_socket(raw, server_side=True)
                try:
                    tls.settimeout(5.0)
                    buf = bytearray()
                    while b"\r\n\r\n" not in buf:
                        buf.extend(tls.recv(4096))
                    # Read request body if Content-Length present
                    head, _, rest = bytes(buf).partition(b"\r\n\r\n")
                    cl = 0
                    for line in head.decode("iso-8859-1").splitlines()[1:]:
                        if line.lower().startswith("content-length:"):
                            cl = int(line.split(":", 1)[1].strip())
                    while len(rest) < cl:
                        rest += tls.recv(4096)
                    resp = (
                        f"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: {len(png_body)}\r\nConnection: close\r\n\r\n"
                    ).encode("ascii") + png_body
                    tls.sendall(resp)
                finally:
                    tls.close()

            def _run_proxy() -> None:
                raw, _ = proxy_sock.accept()
                outer = proxy_srv_ctx.wrap_socket(raw, server_side=True)
                upstream: _socket.socket | None = None
                try:
                    outer.settimeout(5.0)
                    req_buf = bytearray()
                    while not req_buf.endswith(b"\r\n\r\n"):
                        ch = outer.recv(1)
                        if not ch:
                            return
                        req_buf.extend(ch)
                    upstream = _socket.create_connection(("127.0.0.1", dest_port), timeout=5.0)
                    outer.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

                    def _pipe(src: _socket.socket, dst: _socket.socket) -> None:
                        try:
                            while True:
                                data = src.recv(4096)
                                if not data:
                                    break
                                dst.sendall(data)
                        except OSError:
                            pass

                    t1 = _threading.Thread(target=_pipe, args=(outer, upstream), daemon=True)
                    t2 = _threading.Thread(target=_pipe, args=(upstream, outer), daemon=True)
                    t1.start()
                    t2.start()
                    t1.join(timeout=5.0)
                    t2.join(timeout=5.0)
                finally:
                    if upstream is not None:
                        upstream.close()
                    outer.close()

            td = _threading.Thread(target=_run_dest, daemon=True)
            tp = _threading.Thread(target=_run_proxy, daemon=True)
            td.start()
            tp.start()
            try:
                client_ctx = _ssl.create_default_context(cafile=str(ca_bundle))
                with (
                    mock.patch.object(gui, "_default_ssl_context", return_value=client_ctx),
                    mock.patch.object(
                        gui.urllib.request,
                        "getproxies",
                        return_value={"https": f"https://127.0.0.1:{proxy_port}"},
                    ),
                    mock.patch.object(gui.urllib.request, "proxy_bypass_environment", return_value=False),
                    mock.patch.object(gui.urllib.request, "proxy_bypass", return_value=False),
                ):
                    req = gui.urllib.request.Request(
                        f"https://capture.example:{dest_port}/v1/render",
                        data=b'{"url":"https://example.com","output":"png"}',
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with gui._open_capture(req, timeout=5.0, cancel=None) as resp:
                        self.assertEqual(resp.status, 200)
                        self.assertEqual(resp.read(128), png_body)
            finally:
                td.join(timeout=3.0)
                tp.join(timeout=3.0)
                dest_sock.close()
                proxy_sock.close()
