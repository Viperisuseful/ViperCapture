import asyncio
import io
import json
import os
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from vipercapture.render_contract import OutputFormat, RenderRequest
from vipercapture.render_engine import (
    RenderArtifact,
    RenderLimits,
    _ffmpeg_executable,
    _encode_scrolling_media,
    _redact_trace_archive,
    _trim_webm,
    _transcode_video,
    _warc_document,
    diagnostic_bundle,
    diagnostic_url,
)
from vipercapture.render_errors import RenderError


class DiagnosticsAndVideoTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_page_gif_scrolls_over_duration_with_opaque_padding(self):
        process = AsyncMock(return_value=(0, b""))
        with (
            patch("vipercapture.render_engine._ffmpeg_executable", return_value=Path("ffmpeg")),
            patch("vipercapture.render_engine._run_process", process),
        ):
            await _encode_scrolling_media(
                Path("page.png"),
                Path("capture.gif"),
                OutputFormat.GIF,
                width=320,
                height=240,
                duration_ms=5_000,
                transparent=False,
            )
        command = process.await_args.args[0]
        filters = command[command.index("-filter_complex") + 1]
        self.assertIn("(ih-oh)*min(t/5.000,1)", filters)
        self.assertIn("color=black", filters)

    async def test_transparent_full_page_webm_uses_alpha_encoder_and_padding(self):
        process = AsyncMock(return_value=(0, b""))
        with (
            patch("vipercapture.render_engine._ffmpeg_executable", return_value=Path("ffmpeg")),
            patch("vipercapture.render_engine.ffmpeg_has_encoder", return_value=True),
            patch("vipercapture.render_engine._run_process", process),
        ):
            await _encode_scrolling_media(
                Path("page.png"),
                Path("capture.webm"),
                OutputFormat.WEBM,
                width=320,
                height=240,
                duration_ms=1_000,
                transparent=True,
            )
        command = process.await_args.args[0]
        self.assertIn("libvpx-vp9", command)
        self.assertIn("yuva420p", command)
        self.assertEqual(command[command.index("-crf") + 1], "12")
        self.assertEqual(command[command.index("-b:v") + 1], "8M")
        self.assertEqual(command[command.index("-maxrate") + 1], "12M")
        self.assertEqual(command[command.index("-cpu-used") + 1], "4")
        self.assertIn("color=black@0", command[command.index("-vf") + 1])

    async def test_live_webm_trim_uses_high_bitrate_constrained_quality(self):
        process = AsyncMock(return_value=(0, b""))
        duration = AsyncMock(side_effect=[2_000, 1_000])
        with (
            patch("vipercapture.render_engine._ffmpeg_executable", return_value=Path("ffmpeg")),
            patch("vipercapture.render_engine._webm_duration_ms", duration),
            patch("vipercapture.render_engine._run_process", process),
        ):
            await _trim_webm(
                Path("source.webm"), Path("trimmed.webm"), duration_ms=1_000
            )
        command = process.await_args.args[0]
        self.assertEqual(command[command.index("-crf") + 1], "12")
        self.assertEqual(command[command.index("-b:v") + 1], "8M")
        self.assertEqual(command[command.index("-maxrate") + 1], "12M")
        self.assertEqual(command[command.index("-cpu-used") + 1], "4")

    async def test_mp4_transcode_pads_odd_dimensions(self):
        process = AsyncMock(return_value=(0, b""))
        with (
            patch("vipercapture.render_engine._ffmpeg_executable", return_value=Path("ffmpeg")),
            patch("vipercapture.render_engine.ffmpeg_has_encoder", return_value=True),
            patch("vipercapture.render_engine._run_process", process),
        ):
            await _transcode_video(
                Path("capture.webm"), Path("capture.mp4"), OutputFormat.MP4
            )
        command = process.await_args.args[0]
        self.assertIn("pad=ceil(iw/2)*2:ceil(ih/2)*2", command)
        self.assertEqual(command[command.index("-crf") + 1], "17")
        self.assertEqual(command[command.index("-preset") + 1], "fast")

    async def test_gif_transcode_keeps_source_size_and_uses_generated_palette(self):
        process = AsyncMock(return_value=(0, b""))
        with (
            patch("vipercapture.render_engine._ffmpeg_executable", return_value=Path("ffmpeg")),
            patch("vipercapture.render_engine._run_process", process),
        ):
            await _transcode_video(
                Path("capture.webm"), Path("capture.gif"), OutputFormat.GIF
            )
        command = process.await_args.args[0]
        filters = command[command.index("-filter_complex") + 1]
        self.assertIn("fps=15", filters)
        self.assertIn("palettegen", filters)
        self.assertIn("paletteuse", filters)
        self.assertNotIn("scale=", filters)

    async def test_mp4_requires_libx264(self):
        with (
            patch("vipercapture.render_engine._ffmpeg_executable", return_value=Path("ffmpeg")),
            patch("vipercapture.render_engine.ffmpeg_has_encoder", return_value=False),
            self.assertRaises(RenderError) as raised,
        ):
            await _transcode_video(
                Path("capture.webm"), Path("capture.mp4"), OutputFormat.MP4
            )
        self.assertEqual(raised.exception.code, "video_encoder_unavailable")
        self.assertFalse(raised.exception.retryable)

    def test_ffmpeg_discovery_checks_native_playwright_caches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidates = (
                root / "Library" / "Caches" / "ms-playwright" / "ffmpeg-1" / "ffmpeg-mac",
                root / "local" / "ms-playwright" / "ffmpeg-1" / "ffmpeg-win64.exe",
            )
            for candidate in candidates:
                candidate.parent.mkdir(parents=True)
                candidate.write_bytes(b"ffmpeg")
                candidate.chmod(0o700)

            with (
                patch("vipercapture.render_engine.shutil.which", return_value=None),
                patch("vipercapture.render_engine.Path.home", return_value=root),
                patch.dict(os.environ, {"LOCALAPPDATA": str(root / "local")}, clear=True),
            ):
                self.assertEqual(_ffmpeg_executable(), candidates[0])

            candidates[0].unlink()
            with (
                patch("vipercapture.render_engine.shutil.which", return_value=None),
                patch("vipercapture.render_engine.Path.home", return_value=root),
                patch.dict(os.environ, {"LOCALAPPDATA": str(root / "local")}, clear=True),
            ):
                self.assertEqual(_ffmpeg_executable(), candidates[1])

    def test_diagnostic_urls_drop_secrets(self):
        sanitized = diagnostic_url("https://user:pass@example.com/path?token=secret#fragment")
        self.assertEqual(sanitized, "https://example.com/path")

    def test_warc_has_required_fields_and_trace_drops_sensitive_data(self):
        warc = _warc_document([{"url": "https://example.com", "status": 200}])
        self.assertEqual(warc.count(b"WARC-Date:"), 2)
        self.assertEqual(warc.count(b"WARC-Record-ID: <urn:uuid:"), 2)
        source = io.BytesIO()
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr(
                "trace.trace",
                json.dumps(
                    {
                        "headers": [{"name": "Authorization", "value": "secret"}],
                        "url": "https://example.com/path?token=secret",
                        "value": "password",
                    }
                ),
            )
            archive.writestr("trace.network", b"secret network body")
            archive.writestr("resources/body", b"secret response body")
        sanitized = _redact_trace_archive(source.getvalue(), 1024 * 1024)
        self.assertNotIn(b"secret", sanitized)
        with zipfile.ZipFile(io.BytesIO(sanitized)) as archive:
            self.assertEqual(archive.namelist(), ["trace.trace"])
            trace = archive.read("trace.trace")
            self.assertIn(b"[redacted]", trace)
            self.assertNotIn(b"token=", trace)

        with self.assertRaises(RenderError) as raised:
            _redact_trace_archive(source.getvalue(), 1)
        self.assertEqual(raised.exception.code, "output_too_large")

    async def test_diagnostic_bundle_is_self_describing(self):
        request = RenderRequest(
            html="<p>test</p>",
            diagnostics={"bundle": True},
        )
        artifact = RenderArtifact(
            b"image",
            "image/png",
            "capture.png",
            {"width": 1, "final_url": "https://example.com/path?token=secret"},
        )
        result = await diagnostic_bundle(
            artifact,
            request,
            [{"type": "log", "text": "ready"}],
            [{"method": "GET", "url": "https://example.com", "status": 200}],
            RenderLimits(),
        )
        self.assertEqual(result.media_type, "application/zip")
        with zipfile.ZipFile(io.BytesIO(result.body)) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {"capture.png", "manifest.json", "console.json", "network.json"},
            )
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["artifact"]["bytes"], 5)
            self.assertEqual(manifest["artifact"]["metadata"]["final_url"], "https://example.com/path")
            self.assertNotIn(b"secret", archive.read("manifest.json"))

    async def test_cancelled_diagnostic_bundle_settles_zip_thread(self):
        started = threading.Event()
        release = threading.Event()

        def blocked_zip(_entries):
            started.set()
            release.wait(timeout=2)
            return b"zip"

        request = RenderRequest(
            html="<p>test</p>", diagnostics={"bundle": True}
        )
        with patch("vipercapture.render_engine._write_diagnostic_zip", side_effect=blocked_zip):
            operation = asyncio.create_task(
                diagnostic_bundle(
                    RenderArtifact(b"image", "image/png", "capture.png"),
                    request,
                    [],
                    [],
                    RenderLimits(),
                )
            )
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            operation.cancel()
            await asyncio.sleep(0)
            self.assertFalse(operation.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await operation

    def test_video_contract_defaults_and_rejects_invalid_combinations(self):
        request = RenderRequest(url="https://example.com", output="webm")
        self.assertEqual(request.video.duration_ms, 5_000)
        self.assertEqual(request.credit_cost, 1)
        with self.assertRaises(ValidationError):
            RenderRequest(url="https://example.com", output="png", video={})
        with self.assertRaises(ValidationError):
            RenderRequest(
                url="https://example.com",
                output="webm",
                viewports=[
                    {"name": "one", "width": 10, "height": 10},
                    {"name": "two", "width": 10, "height": 10},
                ],
            )
        with self.assertRaises(ValidationError):
            RenderRequest(
                url="https://example.com",
                output="mp4",
                video={"transparent_background": True},
            )
        with self.assertRaises(ValidationError):
            RenderRequest(
                url="https://example.com",
                output="gif",
                full_page=False,
                video={"transparent_background": True},
            )


if __name__ == "__main__":
    unittest.main()
