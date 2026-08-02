import tempfile
import unittest
from pathlib import Path

from render_cache import RenderCache
from render_contract import RenderRequest
from render_engine import RenderArtifact


class RenderCacheTests(unittest.IsolatedAsyncioTestCase):
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
