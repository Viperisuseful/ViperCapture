import asyncio
import tempfile
import threading
import unittest
from pathlib import Path

from render_cache import RenderCache
from render_contract import RenderRequest
from render_engine import RenderArtifact


class RenderCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_removes_interrupted_publication_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            orphan = root / ("a" * 64 + ".bin")
            temporary = root / ".cache-abandoned"
            orphan.write_bytes(b"orphan")
            temporary.write_bytes(b"temporary")

            await RenderCache(root).start()

            self.assertFalse(orphan.exists())
            self.assertFalse(temporary.exists())

    async def test_cancelled_read_settles_before_releasing_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = RenderCache(Path(directory))
            await cache.start()
            request = RenderRequest(html="one", cache=True)
            await cache.put(
                request,
                RenderArtifact(b"png", "image/png", "capture.png"),
            )
            started = threading.Event()
            release = threading.Event()
            original_read = cache._read

            def slow_read(*args):
                started.set()
                release.wait()
                return original_read(*args)

            cache._read = slow_read
            task = asyncio.create_task(cache.get(request))
            await asyncio.to_thread(started.wait)
            task.cancel()
            await asyncio.sleep(0)
            self.assertTrue(cache.lock.locked())
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(cache.lock.locked())

    async def test_exact_round_trip_and_no_plaintext_request_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = RenderCache(Path(directory))
            await cache.start()
            request = RenderRequest(url="https://example.com/private?token=secret", cache=True)
            artifact = RenderArtifact(
                b"png",
                "image/png",
                "capture.png",
                {"width": 10, "final_url": "https://example.com/private?token=secret"},
            )
            self.assertIsNone(await cache.get(request))
            await cache.put(request, artifact)
            hit = await cache.get(request)
            self.assertEqual(hit.body, b"png")
            metadata = b"".join(path.read_bytes() for path in Path(directory).glob("*.json"))
            self.assertNotIn(b"example.com", metadata)
            self.assertNotIn(b"secret", metadata)
            self.assertNotIn(b"final_url", metadata)

    async def test_request_changes_key(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = RenderCache(Path(directory))
            await cache.start()
            first = RenderRequest(html="one", cache=True)
            second = RenderRequest(html="two", cache=True)
            self.assertNotEqual(cache.key(first), cache.key(second))

    async def test_security_mode_changes_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self_hosted = RenderCache(
                root, security_namespace="hosted=0;scripts=1"
            )
            await self_hosted.start()
            hosted = RenderCache(
                root,
                security_namespace="hosted=1;scripts=0;max_pixels=1000000",
            )
            await hosted.start()
            request = RenderRequest(url="https://example.com", cache=True)

            self.assertNotEqual(
                self_hosted.key(request), hosted.key(request)
            )

    async def test_pixel_limit_changes_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            larger = RenderCache(
                root,
                security_namespace="hosted=1;scripts=0;max_pixels=200",
            )
            smaller = RenderCache(
                root,
                security_namespace="hosted=1;scripts=0;max_pixels=100",
            )
            await larger.start()
            await smaller.start()
            request = RenderRequest(url="https://example.com", cache=True)

            self.assertNotEqual(larger.key(request), smaller.key(request))

    async def test_total_byte_budget_evicts_oldest_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = RenderCache(Path(directory), max_bytes=700)
            await cache.start()
            first = RenderRequest(html="one", cache=True)
            second = RenderRequest(html="two", cache=True)
            artifact = RenderArtifact(b"x" * 400, "image/png", "capture.png")
            await cache.put(first, artifact)
            await cache.put(second, artifact)
            self.assertIsNone(await cache.get(first))
            self.assertIsNotNone(await cache.get(second))


if __name__ == "__main__":
    unittest.main()
