"""Bundled S3-compatible async-job artifact provider for S3, R2, and MinIO."""

from __future__ import annotations

import asyncio
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime, timezone
import os
from pathlib import PurePosixPath
import re
from uuid import UUID, uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from async_jobs import Artifact, ArtifactStoreConfig, StoredArtifact


UTC = timezone.utc
NOT_FOUND_CODES = {"NoSuchKey", "404", "NotFound"}
OWNER_METADATA = "vipercapture-v1"


def _safe_extension(filename: str) -> str:
    suffix = PurePosixPath(filename).suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) else ".bin"


def _encode_filename(filename: str) -> str:
    return urlsafe_b64encode(filename.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_filename(value: str, fallback: str) -> str:
    try:
        return urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
    except Exception:
        return fallback


class S3ArtifactStore:
    def __init__(
        self,
        config: ArtifactStoreConfig,
        *,
        bucket: str,
        prefix: str = "vipercapture",
        client=None,
    ) -> None:
        if not bucket:
            raise ValueError("VIPERCAPTURE_S3_BUCKET is required")
        self.config = config
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.client = client or boto3.client(
            "s3",
            endpoint_url=os.getenv("VIPERCAPTURE_S3_ENDPOINT_URL") or None,
            region_name=os.getenv("VIPERCAPTURE_S3_REGION") or None,
            config=Config(
                signature_version="s3v4",
                s3={
                    "addressing_style": os.getenv(
                        "VIPERCAPTURE_S3_ADDRESSING_STYLE", "auto"
                    )
                },
            ),
        )

    async def start(self) -> None:
        await asyncio.to_thread(self.client.head_bucket, Bucket=self.bucket)

    async def close(self) -> None:
        close = getattr(self.client, "close", None)
        if close is not None:
            await asyncio.to_thread(close)

    async def put(
        self,
        job_id: str,
        body: bytes,
        *,
        media_type: str,
        filename: str,
    ) -> StoredArtifact:
        try:
            normalized_job_id = str(UUID(job_id))
        except ValueError as exc:
            raise ValueError("job_id must be a UUID") from exc
        expires_at = datetime.now(UTC) + self.config.result_ttl
        name = f"{uuid4().hex}{_safe_extension(filename)}"
        key = "/".join(
            part for part in (self.prefix, normalized_job_id, name) if part
        )
        await asyncio.to_thread(
            self.client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType=media_type,
            Metadata={
                "filename": _encode_filename(filename),
                "expires": str(int(expires_at.timestamp())),
                "owner": OWNER_METADATA,
            },
        )
        return StoredArtifact(key=key, expires_at=expires_at)

    async def get(self, key: str) -> Artifact | None:
        try:
            response = await asyncio.to_thread(
                self.client.get_object,
                Bucket=self.bucket,
                Key=key,
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in NOT_FOUND_CODES:
                return None
            raise
        metadata = response.get("Metadata", {})
        try:
            expires = int(metadata.get("expires", "0"))
        except ValueError:
            expires = 0
        if not expires or expires <= int(datetime.now(UTC).timestamp()):
            close = getattr(response.get("Body"), "close", None)
            if close is not None:
                await asyncio.to_thread(close)
            await self.delete(key)
            return None
        stream = response["Body"]
        try:
            body = await asyncio.to_thread(stream.read)
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                await asyncio.to_thread(close)
        fallback = PurePosixPath(key).name
        return Artifact(
            key=key,
            body=body,
            media_type=response.get("ContentType") or "application/octet-stream",
            filename=_decode_filename(metadata.get("filename", ""), fallback),
        )

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(
            self.client.delete_object,
            Bucket=self.bucket,
            Key=key,
        )

    async def maintain(self, now: datetime) -> None:
        continuation = None
        while True:
            arguments = {
                "Bucket": self.bucket,
                "Prefix": f"{self.prefix}/" if self.prefix else "",
            }
            if continuation:
                arguments["ContinuationToken"] = continuation
            response = await asyncio.to_thread(self.client.list_objects_v2, **arguments)
            for item in response.get("Contents", []):
                key = item.get("Key")
                if not isinstance(key, str) or not self._owned_key_shape(key):
                    continue
                try:
                    head = await asyncio.to_thread(
                        self.client.head_object,
                        Bucket=self.bucket,
                        Key=key,
                    )
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") in NOT_FOUND_CODES:
                        continue
                    raise
                metadata = head.get("Metadata", {})
                try:
                    expires = int(metadata.get("expires", "0"))
                except (TypeError, ValueError):
                    continue
                if (
                    metadata.get("owner") == OWNER_METADATA
                    and expires > 0
                    and expires <= int(now.timestamp())
                ):
                    await self.delete(key)
            if not response.get("IsTruncated"):
                break
            continuation = response.get("NextContinuationToken")

    def _owned_key_shape(self, key: str) -> bool:
        relative = key
        if self.prefix:
            marker = f"{self.prefix}/"
            if not relative.startswith(marker):
                return False
            relative = relative[len(marker):]
        parts = relative.split("/")
        if len(parts) != 2:
            return False
        try:
            if str(UUID(parts[0])) != parts[0].lower():
                return False
        except ValueError:
            return False
        return bool(re.fullmatch(r"[0-9a-f]{32}\.[a-z0-9]{1,10}", parts[1]))


def create_s3_artifact_store(config: ArtifactStoreConfig) -> S3ArtifactStore:
    """Factory used by VIPERCAPTURE_ARTIFACT_STORE_FACTORY."""
    return S3ArtifactStore(
        config,
        bucket=os.environ["VIPERCAPTURE_S3_BUCKET"],
        prefix=os.getenv("VIPERCAPTURE_S3_PREFIX", "vipercapture"),
    )
