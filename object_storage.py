"""Optional S3-compatible storage for durable hosted files."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


class ObjectStorage:
    """Read and write private application files locally or in an S3 bucket."""

    def __init__(
        self,
        endpoint_url: str | None = None,
        bucket: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        region: str = "auto",
    ) -> None:
        values = (endpoint_url, bucket, access_key_id, secret_access_key)
        if any(values) and not all(values):
            raise ValueError("S3 endpoint, bucket, access key, and secret key must all be set")
        self.bucket = bucket
        self.client = (
            boto3.client(
                "s3",
                endpoint_url=endpoint_url,
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
                region_name=region,
            )
            if all(values)
            else None
        )

    @classmethod
    def from_environment(cls) -> ObjectStorage:
        return cls(
            endpoint_url=os.environ.get("OTTV2_S3_ENDPOINT"),
            bucket=os.environ.get("OTTV2_S3_BUCKET"),
            access_key_id=os.environ.get("OTTV2_S3_ACCESS_KEY_ID"),
            secret_access_key=os.environ.get("OTTV2_S3_SECRET_ACCESS_KEY"),
            region=os.environ.get("OTTV2_S3_REGION", "auto"),
        )

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def put_path(
        self,
        path: str | Path,
        root: str | Path,
        content_type: str = "application/octet-stream",
    ) -> None:
        """Upload an existing file using a key constrained to its storage root."""
        if self.client is None:
            return
        file_path = Path(path).resolve()
        key = self._key(file_path, Path(root).resolve())
        with file_path.open("rb") as source:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=source,
                ContentType=content_type,
            )

    def read_path(self, path: str | Path, root: str | Path) -> bytes:
        """Read a file from the bucket when enabled, or from local disk otherwise."""
        file_path = Path(path).resolve()
        if self.client is None:
            return file_path.read_bytes()
        key = self._key(file_path, Path(root).resolve())
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            error_code = str(exc.response.get("Error", {}).get("Code", ""))
            if error_code in {"NoSuchKey", "404", "NotFound"}:
                raise FileNotFoundError(file_path) from exc
            raise
        body = response["Body"]
        try:
            return body.read()
        finally:
            body.close()

    def ensure_local(self, path: str | Path, root: str | Path) -> Path:
        """Materialize a durable object locally for file-based replay readers."""
        file_path = Path(path).resolve()
        if self.client is None:
            return file_path
        contents = self.read_path(file_path, root)
        file_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{file_path.name}.", dir=file_path.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(contents)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_name, file_path)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
        return file_path

    @staticmethod
    def _key(path: Path, root: Path) -> str:
        try:
            relative_path = path.relative_to(root)
        except ValueError as exc:
            raise ValueError("Object storage path must remain inside its configured root") from exc
        key = relative_path.as_posix()
        if not key or key.startswith("../"):
            raise ValueError("Object storage key is invalid")
        return key
