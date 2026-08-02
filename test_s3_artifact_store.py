import io
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from async_jobs import ArtifactStoreConfig
from s3_artifact_store import S3ArtifactStore


UTC = timezone.utc


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.closed = False

    def head_bucket(self, **_kwargs):
        return {}

    def put_object(self, *, Key, Body, ContentType, Metadata, **_kwargs):
        self.objects[Key] = {
            "Body": bytes(Body),
            "ContentType": ContentType,
            "Metadata": Metadata,
            "LastModified": datetime.now(UTC),
        }

    def get_object(self, *, Key, **_kwargs):
        item = self.objects[Key]
        return {
            "Body": io.BytesIO(item["Body"]),
            "ContentType": item["ContentType"],
            "Metadata": item["Metadata"],
        }

    def head_object(self, *, Key, **_kwargs):
        item = self.objects[Key]
        return {"Metadata": item["Metadata"]}

    def delete_object(self, *, Key, **_kwargs):
        self.objects.pop(Key, None)

    def list_objects_v2(self, **_kwargs):
        return {
            "Contents": [
                {"Key": key, "LastModified": item["LastModified"]}
                for key, item in self.objects.items()
            ],
            "IsTruncated": False,
        }

    def close(self):
        self.closed = True


class S3ArtifactStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_round_trip_and_delete(self):
        client = FakeS3()
        store = S3ArtifactStore(
            ArtifactStoreConfig(Path("/tmp/unused"), timedelta(hours=1)),
            bucket="captures",
            prefix="results",
            client=client,
        )
        await store.start()
        stored = await store.put(
            str(uuid4()),
            b"image-data",
            media_type="image/png",
            filename="capture.png",
        )
        self.assertTrue(stored.key.startswith("results/"))
        self.assertEqual(len(stored.key.split("/")), 3)
        artifact = await store.get(stored.key)
        self.assertEqual(artifact.body, b"image-data")
        self.assertEqual(artifact.filename, "capture.png")
        await store.delete(stored.key)
        self.assertNotIn(stored.key, client.objects)
        await store.close()
        self.assertTrue(client.closed)

    async def test_maintenance_removes_stale_objects(self):
        client = FakeS3()
        store = S3ArtifactStore(
            ArtifactStoreConfig(Path("/tmp/unused"), timedelta(seconds=10)),
            bucket="captures",
            client=client,
        )
        stored = await store.put(
            str(uuid4()),
            b"data",
            media_type="application/octet-stream",
            filename="artifact.bin",
        )
        client.objects[stored.key]["LastModified"] = datetime.now(UTC) - timedelta(minutes=1)
        client.objects[stored.key]["Metadata"]["expires"] = "1"
        await store.maintain(datetime.now(UTC))
        self.assertNotIn(stored.key, client.objects)

    async def test_maintenance_preserves_unowned_objects_under_shared_prefix(self):
        client = FakeS3()
        store = S3ArtifactStore(
            ArtifactStoreConfig(Path("/tmp/unused"), timedelta(seconds=10)),
            bucket="captures",
            prefix="shared",
            client=client,
        )
        job_id = str(uuid4())
        artifact_id = uuid4().hex
        unrelated = f"shared/{job_id}/{artifact_id}.bin"
        client.objects[unrelated] = {
            "Body": b"other application",
            "ContentType": "application/octet-stream",
            "Metadata": {"expires": "1"},
            "LastModified": datetime.now(UTC) - timedelta(days=1),
        }
        malformed = "shared/not-a-job/object.bin"
        client.objects[malformed] = dict(client.objects[unrelated])

        await store.maintain(datetime.now(UTC))

        self.assertIn(unrelated, client.objects)
        self.assertIn(malformed, client.objects)
