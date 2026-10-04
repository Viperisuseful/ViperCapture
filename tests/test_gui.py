from __future__ import annotations

import io
import json
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
